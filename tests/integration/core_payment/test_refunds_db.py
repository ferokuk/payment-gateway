"""Refund invariants against a real PostgreSQL.

Run: docker compose up -d database, then
  $env:TEST_DATABASE_URL = "postgresql+asyncpg://postgres:postgres@localhost:5432/payment_gateway_test"
  uv run pytest tests/integration/core_payment/test_refunds_db.py -v
Tables are created and dropped wholesale — the DB must be dedicated to tests.
"""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import anyio
import pytest
from dishka import Provider, Scope, make_async_container, provide
from dishka.integrations.fastapi import FastapiProvider, setup_dishka
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError
from src.contexts.core_payment.application.dto.refund import ReconciliationReportDTO
from src.contexts.core_payment.application.use_cases.reconcile_stuck_refunds import (
    UNRESOLVED_STATUSES,
    ReconcileStuckRefundsUseCase,
)
from src.contexts.core_payment.domain.exceptions import StaleRefundStateError
from src.contexts.core_payment.domain.refund import Refund
from src.contexts.core_payment.domain.statuses import RefundFailureReasons, RefundStatuses
from src.contexts.core_payment.infrastructure.database.models import (
    PaymentModel,
    RefundIdempotencyKeyModel,
    RefundModel,
)
from src.contexts.core_payment.infrastructure.database.repositories import (
    SQLAlchemyPaymentRepository,
    SQLAlchemyRefundIdempotencyKeyRepository,
    SQLAlchemyRefundRepository,
)
from src.contexts.core_payment.infrastructure.providers.base import (
    PaymentProvider,
    ProviderInitiationError,
    ProviderRejectedError,
    RefundProviderState,
    RefundProviderStatus,
)
from src.contexts.core_payment.ioc import CorePaymentProvider, SystemCorePaymentProvider
from src.contexts.core_payment.presentation.routers.callbacks import (
    router as callbacks_router,
)
from src.contexts.core_payment.presentation.routers.payment import router as payment_router
from src.contexts.core_payment.presentation.routers.refund import router as refund_router
from src.shared.config import Settings
from src.shared.database.database import Base
from src.shared.database.engine import create_engine, create_sessionmaker
from src.shared.ioc import DatabaseProvider, RepositoriesProvider, SystemRepositoriesProvider
from src.shared.security import AuthProvider
from tests.fixtures.client import API_KEY, CALLBACK_SECRET, FakePaymentProviderProvider
from tests.fixtures.merchants import MERCHANT_ID, seed_legacy_merchant
from tests.fixtures.providers import RecordingFakeProvider

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    TEST_DATABASE_URL is None,
    reason="TEST_DATABASE_URL is not set — real-DB tests run only locally",
)


class _DbConfigProvider(Provider):
    scope = Scope.APP

    @provide
    def get_settings(self) -> Settings:
        assert TEST_DATABASE_URL is not None
        return Settings(
            database_url=TEST_DATABASE_URL,
            api_key=API_KEY,
            callback_secret=CALLBACK_SECRET,
        )


@pytest.fixture
async def _tables() -> AsyncIterator[None]:
    assert TEST_DATABASE_URL is not None
    engine = create_engine(TEST_DATABASE_URL)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
        await seed_legacy_merchant(conn)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@asynccontextmanager
async def _db_client(provider: PaymentProvider | None = None) -> AsyncIterator[AsyncClient]:
    app = FastAPI()
    app.include_router(payment_router)
    app.include_router(callbacks_router)
    app.include_router(refund_router)
    container = make_async_container(
        _DbConfigProvider(),
        DatabaseProvider(),
        RepositoriesProvider(),
        SystemRepositoriesProvider(),
        FakePaymentProviderProvider(provider if provider is not None else RecordingFakeProvider()),
        CorePaymentProvider(),
        SystemCorePaymentProvider(),
        AuthProvider(),
        FastapiProvider(),
    )
    setup_dishka(container, app)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    await container.close()


async def _create_success_payment(client: AsyncClient) -> str:
    response = await client.post(
        "/payments",
        json={"amount": "100.50", "currency": "USD", "provider_id": 1},
        headers={"X-API-Key": API_KEY},
    )
    assert response.status_code == 201
    payment_id = str(response.json()["payment_id"])
    for body in (
        {"payment_id": payment_id, "status": "processing"},
        {"payment_id": payment_id, "status": "success"},
    ):
        callback = await client.post(
            "/callbacks/payments", json=body, headers={"X-Callback-Secret": CALLBACK_SECRET}
        )
        assert callback.status_code == 200
    return payment_id


async def _post_refund(client: AsyncClient, payment_id: str, amount: str) -> Response:
    return await client.post(
        f"/payments/{payment_id}/refunds",
        json={"amount": amount},
        headers={"X-API-Key": API_KEY},
    )


async def _refunded_amount(payment_id: str) -> Decimal:
    """Read with an independent engine: only committed data is visible."""
    assert TEST_DATABASE_URL is not None
    engine = create_engine(TEST_DATABASE_URL)
    maker = create_sessionmaker(engine)
    async with maker() as session:
        value = (
            await session.execute(
                select(PaymentModel.refunded_amount).where(PaymentModel.id == UUID(payment_id))
            )
        ).scalar_one()
    await engine.dispose()
    return value


async def _refund_statuses() -> list[str]:
    assert TEST_DATABASE_URL is not None
    engine = create_engine(TEST_DATABASE_URL)
    maker = create_sessionmaker(engine)
    async with maker() as session:
        statuses = list((await session.execute(select(RefundModel.status))).scalars())
    await engine.dispose()
    return [status.value for status in statuses]


@pytest.mark.anyio
async def test_concurrent_reservations_only_one_wins(_tables: None) -> None:
    # The main invariant test: 60 + 60 on a 100.50 payment — exactly one succeeds.
    async with _db_client() as client:
        payment_id = await _create_success_payment(client)
        responses: list[Response] = []

        async def _call() -> None:
            responses.append(await _post_refund(client, payment_id, "60.00"))

        async with anyio.create_task_group() as tg:
            tg.start_soon(_call)
            tg.start_soon(_call)

    assert sorted(response.status_code for response in responses) == [201, 409]
    assert await _refunded_amount(payment_id) == Decimal("60.00")
    assert len(await _refund_statuses()) == 1


@pytest.mark.anyio
async def test_concurrent_duplicate_failed_callbacks_release_once(_tables: None) -> None:
    async with _db_client() as client:
        payment_id = await _create_success_payment(client)
        create = await _post_refund(client, payment_id, "60.00")
        refund_id = create.json()["refund_id"]
        responses: list[Response] = []

        async def _call() -> None:
            responses.append(
                await client.post(
                    "/callbacks/refunds",
                    json={
                        "refund_id": refund_id,
                        "status": "failed",
                        "failure_reason": "timeout",
                    },
                    headers={"X-Callback-Secret": CALLBACK_SECRET},
                )
            )

        async with anyio.create_task_group() as tg:
            tg.start_soon(_call)
            tg.start_soon(_call)

    # One callback wins the CAS and releases; the duplicate no-ops with 200.
    assert {response.status_code for response in responses} == {200}
    assert await _refunded_amount(payment_id) == Decimal("0.00")
    assert await _refund_statuses() == ["failed"]


@pytest.mark.anyio
async def test_check_constraint_rejects_bypassing_write(_tables: None) -> None:
    async with _db_client() as client:
        payment_id = await _create_success_payment(client)

    assert TEST_DATABASE_URL is not None
    engine = create_engine(TEST_DATABASE_URL)
    maker = create_sessionmaker(engine)
    async with maker() as session:
        with pytest.raises(IntegrityError):
            await session.execute(text("UPDATE payments SET refunded_amount = amount + 1"))
        await session.rollback()
    await engine.dispose()

    assert await _refunded_amount(payment_id) == Decimal("0.00")


@pytest.mark.anyio
async def test_list_unresolved_sees_only_old_open_refunds(_tables: None) -> None:
    # A refund whose initiation failed stays in created with the reservation
    # held — exactly what reconciliation has to find.
    down = RecordingFakeProvider(refund_error=ProviderInitiationError("provider down"))
    async with _db_client(down) as client:
        payment_id = await _create_success_payment(client)
        stuck = await _post_refund(client, payment_id, "10.00")

    assert stuck.status_code == 502

    assert TEST_DATABASE_URL is not None
    now = datetime.now(UTC)
    engine = create_engine(TEST_DATABASE_URL)
    maker = create_sessionmaker(engine)
    async with maker() as session:
        repo = SQLAlchemyRefundRepository(session, MERCHANT_ID)
        too_young = await repo.list_unresolved(
            statuses=UNRESOLVED_STATUSES,
            created_before=now - timedelta(minutes=15),
            due_before=now,
            limit=10,
        )
        overdue = await repo.list_unresolved(
            statuses=UNRESOLVED_STATUSES, created_before=now, due_before=now, limit=10
        )
        settled_only = await repo.list_unresolved(
            statuses=(RefundStatuses.SUCCESS,), created_before=now, due_before=now, limit=10
        )
    await engine.dispose()

    assert too_young == []
    assert settled_only == []
    assert [item.refund.status for item in overdue] == [RefundStatuses.CREATED]
    assert overdue[0].refund.payment_id == UUID(payment_id)
    assert await _refunded_amount(payment_id) == Decimal("10.00")


async def _reconcile(provider: PaymentProvider, *, batch_size: int = 10) -> ReconciliationReportDTO:
    """One reconciliation pass with its own engine — the reconciler is a
    separate process in production, so it never shares the API's session."""
    assert TEST_DATABASE_URL is not None
    engine = create_engine(TEST_DATABASE_URL)
    maker = create_sessionmaker(engine)
    async with maker() as session:
        use_case = ReconcileStuckRefundsUseCase(
            SQLAlchemyRefundRepository(session, MERCHANT_ID),
            SQLAlchemyPaymentRepository(session, MERCHANT_ID),
            provider,
            session,
            stuck_after=timedelta(seconds=0),
            initiation_max_age=timedelta(days=1),
            batch_size=batch_size,
        )
        report = await use_case()
        await session.commit()
    await engine.dispose()
    return report


async def _stuck_refund(client: AsyncClient, payment_id: str, amount: str) -> None:
    response = await _post_refund(client, payment_id, amount)
    assert response.status_code == 502


@pytest.mark.parametrize(
    "setting_name",
    [
        "REFUND_INITIATION_MAX_AGE_SECONDS",
        "RECONCILE_GIVE_UP_AFTER_SECONDS",
    ],
)
@pytest.mark.anyio
async def test_api_and_worker_share_initiation_window_and_release_only_after_absence(
    _tables: None,
    monkeypatch: pytest.MonkeyPatch,
    setting_name: str,
) -> None:
    # Two hours is inside the default 20h, but outside the configured 1h.
    # Resolve both use cases through real DI to verify the shared setting.
    monkeypatch.delenv("REFUND_INITIATION_MAX_AGE_SECONDS", raising=False)
    monkeypatch.delenv("RECONCILE_GIVE_UP_AFTER_SECONDS", raising=False)
    monkeypatch.setenv(setting_name, "3600")
    headers = {"X-API-Key": API_KEY, "Idempotency-Key": "expired-refund"}
    down = RecordingFakeProvider(refund_error=ProviderInitiationError("down"))
    async with _db_client(down) as client:
        payment_id = await _create_success_payment(client)
        response = await client.post(
            f"/payments/{payment_id}/refunds",
            json={"amount": "40.00"},
            headers=headers,
        )
        assert response.status_code == 502

    assert TEST_DATABASE_URL is not None
    engine = create_engine(TEST_DATABASE_URL)
    try:
        async with create_sessionmaker(engine).begin() as session:
            await session.execute(
                update(RefundModel).values(
                    created_at=datetime.now(UTC) - timedelta(hours=2),
                )
            )
    finally:
        await engine.dispose()

    provider = RecordingFakeProvider(refund_status=RefundProviderStatus(RefundProviderState.ABSENT))
    async with _db_client(provider) as client:
        response = await client.post(
            f"/payments/{payment_id}/refunds",
            json={"amount": "40.00"},
            headers=headers,
        )
    assert response.status_code == 409
    assert "initiation window expired" in response.json()["detail"]
    assert provider.initiated_refunds == []
    assert provider.status_queries == []
    assert await _refund_statuses() == ["created"]
    assert await _refunded_amount(payment_id) == Decimal("40.00")

    container = make_async_container(
        _DbConfigProvider(),
        DatabaseProvider(),
        SystemRepositoriesProvider(),
        FakePaymentProviderProvider(provider),
        SystemCorePaymentProvider(),
    )
    try:
        async with container() as scope:
            use_case = await scope.get(ReconcileStuckRefundsUseCase)
            report = await use_case()
    finally:
        await container.close()
    assert report.closed == 1 and report.resumed == 0
    assert provider.initiated_refunds == []
    assert len(provider.status_queries) == 1
    assert await _refund_statuses() == ["failed"]
    assert await _refunded_amount(payment_id) == Decimal("0.00")
    async with _db_client(provider) as client:
        replay = await client.post(
            f"/payments/{payment_id}/refunds",
            json={"amount": "40.00"},
            headers=headers,
        )
    assert replay.status_code == 201 and replay.json()["status"] == "failed"
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert provider.initiated_refunds == [] and len(provider.status_queries) == 1
    assert await _refunded_amount(payment_id) == Decimal("0.00")


@pytest.mark.anyio
async def test_reconciliation_closes_refused_refund_and_frees_the_remainder(
    _tables: None,
) -> None:
    down = RecordingFakeProvider(refund_error=ProviderInitiationError("provider down"))
    async with _db_client(down) as client:
        payment_id = await _create_success_payment(client)
        await _stuck_refund(client, payment_id, "60.00")

    assert await _refunded_amount(payment_id) == Decimal("60.00")

    report = await _reconcile(
        RecordingFakeProvider(
            refund_status=RefundProviderStatus(RefundProviderState.ABSENT),
            refund_error=ProviderRejectedError("payment too old to refund"),
        )
    )

    assert (report.closed, report.resumed, report.unresolved) == (1, 0, 0)
    assert await _refund_statuses() == ["failed"]
    assert await _refunded_amount(payment_id) == Decimal("0.00")

    # The proof that matters to the merchant: the money is refundable again.
    async with _db_client() as client:
        recovered = await _post_refund(client, payment_id, "60.00")
    assert recovered.status_code == 201


@pytest.mark.anyio
async def test_reconciliation_resumes_refund_the_provider_takes(_tables: None) -> None:
    down = RecordingFakeProvider(refund_error=ProviderInitiationError("provider down"))
    async with _db_client(down) as client:
        payment_id = await _create_success_payment(client)
        await _stuck_refund(client, payment_id, "60.00")

    back_up = RecordingFakeProvider(refund_status=RefundProviderStatus(RefundProviderState.ABSENT))
    report = await _reconcile(back_up)

    assert (report.resumed, report.closed) == (1, 0)
    assert await _refund_statuses() == ["pending"]
    # The reservation stays: the refund is alive at the provider now.
    assert await _refunded_amount(payment_id) == Decimal("60.00")
    assert len(back_up.initiated_refunds) == 1


@pytest.mark.anyio
async def test_reconciliation_settles_a_refund_whose_callback_was_lost(_tables: None) -> None:
    # The refund reached pending and no callback ever came: the reservation is
    # held forever until the provider is asked directly.
    async with _db_client() as client:
        payment_id = await _create_success_payment(client)
        created = await _post_refund(client, payment_id, "60.00")
        assert created.status_code == 201

    assert await _refund_statuses() == ["pending"]

    report = await _reconcile(
        RecordingFakeProvider(
            refund_status=RefundProviderStatus(
                RefundProviderState.FAILED, RefundFailureReasons.CARD_UNAVAILABLE
            )
        )
    )

    assert (report.closed, report.unresolved) == (1, 0)
    assert await _refund_statuses() == ["failed"]
    assert await _refunded_amount(payment_id) == Decimal("0.00")


@pytest.mark.anyio
async def test_reconciliation_settles_a_refund_the_provider_errored_on(_tables: None) -> None:
    async with _db_client() as client:
        payment_id = await _create_success_payment(client)
        created = await _post_refund(client, payment_id, "60.00")
        refund_id = created.json()["refund_id"]
        errored = await client.post(
            "/callbacks/refunds",
            json={
                "refund_id": refund_id,
                "status": "error",
                "error_message": "Internal provider error",
            },
            headers={"X-Callback-Secret": CALLBACK_SECRET},
        )
        assert errored.status_code == 200

    assert await _refund_statuses() == ["error"]
    # The reservation is deliberately held while the outcome is unknown.
    assert await _refunded_amount(payment_id) == Decimal("60.00")

    report = await _reconcile(
        RecordingFakeProvider(refund_status=RefundProviderStatus(RefundProviderState.SUCCEEDED))
    )

    # The provider settled it as success, so the money really did leave.
    assert (report.completed, report.closed) == (1, 0)
    assert await _refund_statuses() == ["success"]
    assert await _refunded_amount(payment_id) == Decimal("60.00")


@pytest.mark.parametrize(
    "head_state",
    [RefundProviderState.UNKNOWN, RefundProviderState.ABSENT, RefundProviderState.PENDING],
)
@pytest.mark.parametrize("delayed_pass", [False, True])
@pytest.mark.anyio
async def test_unresolved_first_batch_does_not_starve_later_refunds_in_postgres(
    _tables: None,
    monkeypatch: pytest.MonkeyPatch,
    head_state: RefundProviderState,
    delayed_pass: bool,
) -> None:
    async with _db_client() as client:
        payment_id = await _create_success_payment(client)
        refund_ids = []
        for _ in range(4):
            response = await _post_refund(client, payment_id, "10.00")
            assert response.status_code == 201
            refund_ids.append(UUID(response.json()["refund_id"]))

    assert TEST_DATABASE_URL is not None
    engine = create_engine(TEST_DATABASE_URL)
    maker = create_sessionmaker(engine)
    now = datetime.now(UTC)
    try:
        async with maker.begin() as session:
            for refund_id, age in zip(refund_ids, [3, 3, 1, 1], strict=True):
                await session.execute(
                    update(RefundModel)
                    .where(RefundModel.id == refund_id)
                    .values(created_at=now - timedelta(days=age))
                )

        class _Provider(RecordingFakeProvider):
            async def get_refund_status(self, refund: Refund) -> RefundProviderStatus:
                self.status_queries.append(refund)
                if refund.id in refund_ids[:2]:
                    return RefundProviderStatus(head_state)
                return RefundProviderStatus(
                    RefundProviderState.FAILED, RefundFailureReasons.TIMEOUT
                )

        provider = _Provider()
        first = await _reconcile(provider, batch_size=2)
        assert first.unresolved + first.disputed == 2
        async with maker() as session:
            rows = list(await session.scalars(select(RefundModel).order_by(RefundModel.id)))
            assert [row.reconciliation_attempts for row in rows] == [1, 1, 0, 0]
            assert all(
                row.next_reconcile_at is not None and row.next_reconcile_at > now
                for row in rows[:2]
            )

        if delayed_pass:

            class _Clock:
                @classmethod
                def now(cls, tz: object = None) -> datetime:
                    return now + timedelta(minutes=2)

            monkeypatch.setattr(
                "src.contexts.core_payment.application.use_cases.reconcile_stuck_refunds.datetime",
                _Clock,
            )

        # A new engine/session simulates restarting the worker. Even if the
        # head is due again, the tail has an earlier scheduled check and wins.
        second = await _reconcile(provider, batch_size=2)
        assert second.closed == 2
        assert [refund.id for refund in provider.status_queries] == refund_ids
        assert await _refunded_amount(payment_id) == Decimal("20.00")
        async with maker() as session:
            rows = list(await session.scalars(select(RefundModel).order_by(RefundModel.id)))
            assert [row.status for row in rows] == [
                RefundStatuses.PENDING,
                RefundStatuses.PENDING,
                RefundStatuses.FAILED,
                RefundStatuses.FAILED,
            ]
            assert [row.reconciliation_attempts for row in rows] == [1, 1, 1, 1]
    finally:
        await engine.dispose()


@pytest.mark.anyio
async def test_concurrent_schedule_claim_has_one_winner(_tables: None) -> None:
    async with _db_client() as client:
        payment_id = await _create_success_payment(client)
        assert (await _post_refund(client, payment_id, "10.00")).status_code == 201
    assert TEST_DATABASE_URL is not None
    engine = create_engine(TEST_DATABASE_URL)
    maker = create_sessionmaker(engine)
    now = datetime.now(UTC)
    try:
        async with maker() as session:
            candidates = await SQLAlchemyRefundRepository(session, MERCHANT_ID).list_unresolved(
                statuses=UNRESOLVED_STATUSES, created_before=now, due_before=now, limit=1
            )
        candidate = candidates[0]
        results: list[bool] = []

        async def claim() -> None:
            async with maker.begin() as session:
                results.append(
                    await SQLAlchemyRefundRepository(session, MERCHANT_ID).schedule_reconciliation(
                        candidate, next_check_at=now + timedelta(minutes=1)
                    )
                )

        async with anyio.create_task_group() as tasks:
            tasks.start_soon(claim)
            tasks.start_soon(claim)
        assert sorted(results) == [False, True]
        async with maker() as session:
            row = await session.get(RefundModel, candidate.refund.id)
            assert row is not None
            assert row.reconciliation_attempts == 1
            assert row.next_reconcile_at == now + timedelta(minutes=1)
            assert row.status is RefundStatuses.PENDING
        assert await _refunded_amount(payment_id) == Decimal("10.00")
    finally:
        await engine.dispose()


@pytest.mark.anyio
async def test_callback_during_status_query_releases_once_and_preserves_schedule(
    _tables: None,
) -> None:
    async with _db_client() as client:
        payment_id = await _create_success_payment(client)
        response = await _post_refund(client, payment_id, "10.00")
        assert response.status_code == 201
        refund_id = UUID(response.json()["refund_id"])

    class _CallbackProvider(RecordingFakeProvider):
        async def get_refund_status(self, refund: Refund) -> RefundProviderStatus:
            # This callback must commit while the status request is in flight:
            # neither the selection nor the claim may hold a database lock.
            async with _db_client() as client:
                response = await client.post(
                    "/callbacks/refunds",
                    json={
                        "refund_id": str(refund.id),
                        "status": "failed",
                        "failure_reason": "timeout",
                    },
                    headers={"X-Callback-Secret": CALLBACK_SECRET},
                )
                assert response.status_code == 200
            return RefundProviderStatus(RefundProviderState.FAILED, RefundFailureReasons.TIMEOUT)

    with anyio.fail_after(10):
        report = await _reconcile(_CallbackProvider())
    assert report.conflicts == 1
    assert report.closed == 0
    assert await _refunded_amount(payment_id) == Decimal("0.00")
    assert TEST_DATABASE_URL is not None
    engine = create_engine(TEST_DATABASE_URL)
    try:
        async with create_sessionmaker(engine)() as session:
            row = await session.get(RefundModel, refund_id)
            assert row is not None and row.status is RefundStatuses.FAILED
            assert row.reconciliation_attempts == 1
            assert row.next_reconcile_at is not None
    finally:
        await engine.dispose()


@pytest.mark.anyio
async def test_list_refunds_reads_only_this_payment_in_creation_order(_tables: None) -> None:
    # The ordering and the payment filter live in SQL, so the fake repository
    # cannot prove them — only a real ORDER BY / WHERE can.
    async with _db_client() as client:
        payment_id = await _create_success_payment(client)
        other_payment_id = await _create_success_payment(client)
        first = (await _post_refund(client, payment_id, "10.00")).json()["refund_id"]
        second = (await _post_refund(client, payment_id, "20.00")).json()["refund_id"]
        foreign = await _post_refund(client, other_payment_id, "30.00")
        listed = await client.get(f"/payments/{payment_id}/refunds", headers={"X-API-Key": API_KEY})

    assert foreign.status_code == 201
    assert listed.status_code == 200
    assert [item["refund_id"] for item in listed.json()["refunds"]] == [first, second]


@pytest.mark.parametrize(
    ("provider_state", "expected_status", "reserved"),
    [
        (RefundProviderState.PENDING, "pending", "40.00"),
        (RefundProviderState.SUCCEEDED, "success", "40.00"),
        (RefundProviderState.FAILED, "failed", "0.00"),
        (RefundProviderState.ABSENT, "failed", "0.00"),
    ],
)
@pytest.mark.anyio
async def test_concurrent_replays_after_reconciliation_restore_one_response(
    _tables: None,
    provider_state: RefundProviderState,
    expected_status: str,
    reserved: str,
) -> None:
    headers = {"X-API-Key": API_KEY, "Idempotency-Key": "reconciled-refund"}
    down = RecordingFakeProvider(refund_error=ProviderInitiationError("down"))
    async with _db_client(down) as client:
        payment_id = await _create_success_payment(client)
        response = await client.post(
            f"/payments/{payment_id}/refunds",
            json={"amount": "40.00"},
            headers=headers,
        )
        assert response.status_code == 502
    assert TEST_DATABASE_URL is not None
    engine = create_engine(TEST_DATABASE_URL)
    try:
        async with create_sessionmaker(engine).begin() as session:
            await session.execute(
                update(RefundModel).values(
                    created_at=datetime.now(UTC) - timedelta(days=3),
                )
            )
        verdict = RefundProviderStatus(
            provider_state,
            RefundFailureReasons.TIMEOUT if provider_state is RefundProviderState.FAILED else None,
        )
        await _reconcile(RecordingFakeProvider(refund_status=verdict))
        assert await _refund_statuses() == [expected_status]

        # Two independent HTTP request scopes see a missing snapshot; neither
        # may call the still unavailable provider or reserve the amount again.
        responses: list[Response] = []
        async with _db_client(down) as client:

            async def replay() -> None:
                responses.append(
                    await client.post(
                        f"/payments/{payment_id}/refunds",
                        json={"amount": "40.00"},
                        headers=headers,
                    )
                )

            async with anyio.create_task_group() as tasks:
                tasks.start_soon(replay)
                tasks.start_soon(replay)
            assert all(response.status_code == 201 for response in responses)
            body = responses[0].json()
            assert body == responses[1].json()
            assert body["status"] == expected_status
            assert all(response.headers["Idempotency-Replayed"] == "true" for response in responses)
            if expected_status == "pending":
                completed = await client.post(
                    "/callbacks/refunds",
                    json={"refund_id": body["refund_id"], "status": "success"},
                    headers={"X-Callback-Secret": CALLBACK_SECRET},
                )
                assert completed.status_code == 200
            await replay()
            assert responses[-1].json() == body
        async with create_sessionmaker(engine)() as session:
            record = await session.get(
                RefundIdempotencyKeyModel, (MERCHANT_ID, "reconciled-refund")
            )
            assert record is not None and record.response_body == body
        assert await _refunded_amount(payment_id) == Decimal(reserved)
        assert len(await _refund_statuses()) == 1
        assert down.initiated_refunds == [] and down.status_queries == []
    finally:
        await engine.dispose()


@pytest.mark.parametrize("sql_null", [False, True])
@pytest.mark.anyio
async def test_competing_snapshots_return_the_first_committed_response(
    _tables: None,
    sql_null: bool,
) -> None:
    # Python None is normally JSON null in JSONB. Legacy/imported keys may
    # instead contain SQL NULL: both representations must be recoverable.
    headers = {"X-API-Key": API_KEY, "Idempotency-Key": "competing-snapshots"}
    down = RecordingFakeProvider(refund_error=ProviderInitiationError("down"))
    async with _db_client(down) as client:
        payment_id = await _create_success_payment(client)
        response = await client.post(
            f"/payments/{payment_id}/refunds",
            json={"amount": "40.00"},
            headers=headers,
        )
        assert response.status_code == 502
    assert TEST_DATABASE_URL is not None
    engine = create_engine(TEST_DATABASE_URL)
    maker = create_sessionmaker(engine)
    try:
        async with maker.begin() as session:
            if sql_null:
                await session.execute(
                    text("UPDATE refund_idempotency_keys SET response_body = NULL")
                )
            record = await session.get(
                RefundIdempotencyKeyModel, (MERCHANT_ID, "competing-snapshots")
            )
            assert record is not None
            refund_id = str(record.refund_id)
        snapshots = [
            {
                "refund_id": refund_id,
                "payment_id": payment_id,
                "amount": "40.00",
                "status": state,
            }
            for state in ("pending", "success")
        ]
        observed: list[dict[str, Any]] = []
        ready = 0
        both_read = anyio.Event()

        async def write_snapshot(body: dict[str, Any]) -> None:
            nonlocal ready
            async with maker.begin() as session:
                repo = SQLAlchemyRefundIdempotencyKeyRepository(session, MERCHANT_ID)
                original = await repo.get("competing-snapshots")
                assert original is not None and original.response_body is None
                ready += 1
                if ready == 2:
                    both_read.set()
                await both_read.wait()
                observed.append(await repo.set_response("competing-snapshots", body))

        with anyio.fail_after(10):
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(write_snapshot, snapshots[0])
                tasks.start_soon(write_snapshot, snapshots[1])
        assert observed[0] == observed[1]
        assert observed[0] in snapshots
        async with maker() as session:
            record = await session.get(
                RefundIdempotencyKeyModel, (MERCHANT_ID, "competing-snapshots")
            )
            assert record is not None and record.response_body == observed[0]
        assert await _refunded_amount(payment_id) == Decimal("40.00")
    finally:
        await engine.dispose()


@pytest.mark.parametrize("with_key", [False, True])
@pytest.mark.parametrize("failed", [False, True])
@pytest.mark.anyio
async def test_reconciliation_winning_api_cas_returns_its_outcome(
    _tables: None,
    with_key: bool,
    failed: bool,
) -> None:
    class _RacingProvider(RecordingFakeProvider):
        async def initiate_refund(self, refund: Refund) -> None:
            await super().initiate_refund(refund)
            state = RefundProviderState.FAILED if failed else RefundProviderState.SUCCEEDED
            verdict = RefundProviderStatus(state, RefundFailureReasons.TIMEOUT if failed else None)
            report = await _reconcile(RecordingFakeProvider(refund_status=verdict))
            assert report.closed + report.completed == 1

    provider = _RacingProvider()
    headers = {"X-API-Key": API_KEY}
    if with_key:
        headers["Idempotency-Key"] = "racing-worker"
    with anyio.fail_after(10):
        async with _db_client(provider) as client:
            payment_id = await _create_success_payment(client)
            response = await client.post(
                f"/payments/{payment_id}/refunds",
                json={"amount": "40.00"},
                headers=headers,
            )
            assert response.status_code == 201
            assert response.json()["status"] == ("failed" if failed else "success")
            if with_key:
                replay = await client.post(
                    f"/payments/{payment_id}/refunds",
                    json={"amount": "40.00"},
                    headers=headers,
                )
                assert replay.status_code == 201 and replay.json() == response.json()
                assert replay.headers["Idempotency-Replayed"] == "true"
    assert len(provider.initiated_refunds) == 1
    assert len(await _refund_statuses()) == 1
    assert await _refunded_amount(payment_id) == (Decimal("0") if failed else Decimal("40.00"))


@pytest.mark.anyio
async def test_stale_refund_transition_does_not_regress(_tables: None) -> None:
    # Regression scenario: the refund has reached SUCCESS, a lagging competitor
    # with a CREATED snapshot tries to write PENDING.
    async with _db_client() as client:
        payment_id = await _create_success_payment(client)
        create = await _post_refund(client, payment_id, "60.00")
        refund_id = create.json()["refund_id"]
        callback = await client.post(
            "/callbacks/refunds",
            json={"refund_id": refund_id, "status": "success"},
            headers={"X-Callback-Secret": CALLBACK_SECRET},
        )
        assert callback.status_code == 200

    assert await _refund_statuses() == ["success"]

    assert TEST_DATABASE_URL is not None
    engine = create_engine(TEST_DATABASE_URL)
    maker = create_sessionmaker(engine)
    async with maker() as session:
        repo = SQLAlchemyRefundRepository(session, MERCHANT_ID)
        stale = await repo.get_by_id(UUID(refund_id))
        assert stale is not None
        # Simulate a stale snapshot: the competitor "remembers" the refund in CREATED.
        stale.status = RefundStatuses.CREATED
        stale.mark_pending()
        with pytest.raises(StaleRefundStateError):
            await repo.update(stale, expected_status=RefundStatuses.CREATED)
        await session.rollback()
    await engine.dispose()

    assert await _refund_statuses() == ["success"]
