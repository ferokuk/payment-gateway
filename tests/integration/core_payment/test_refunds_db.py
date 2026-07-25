"""Refund invariants against a real PostgreSQL.

Run: docker compose up -d database, then
  $env:TEST_DATABASE_URL = "postgresql+asyncpg://postgres:postgres@localhost:5433/payment_gateway_test"
  uv run pytest tests/integration/core_payment/test_refunds_db.py -v
Tables are created and dropped wholesale — the DB must be dedicated to tests.
"""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import anyio
import pytest
from dishka import Provider, Scope, make_async_container, provide
from dishka.integrations.fastapi import FastapiProvider, setup_dishka
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from src.contexts.core_payment.application.dto.refund import ReconciliationReportDTO
from src.contexts.core_payment.application.use_cases.reconcile_stuck_refunds import (
    UNRESOLVED_STATUSES,
    ReconcileStuckRefundsUseCase,
)
from src.contexts.core_payment.domain.exceptions import StaleRefundStateError
from src.contexts.core_payment.domain.statuses import RefundFailureReasons, RefundStatuses
from src.contexts.core_payment.infrastructure.database.models import (
    PaymentModel,
    RefundModel,
)
from src.contexts.core_payment.infrastructure.database.repositories import (
    SQLAlchemyPaymentRepository,
    SQLAlchemyRefundRepository,
)
from src.contexts.core_payment.infrastructure.providers.base import (
    PaymentProvider,
    ProviderInitiationError,
    ProviderRejectedError,
    RefundProviderState,
    RefundProviderStatus,
)
from src.contexts.core_payment.ioc import CorePaymentProvider
from src.contexts.core_payment.presentation.routers.callbacks import (
    router as callbacks_router,
)
from src.contexts.core_payment.presentation.routers.payment import router as payment_router
from src.contexts.core_payment.presentation.routers.refund import router as refund_router
from src.shared.config import Settings
from src.shared.database.database import Base
from src.shared.database.engine import create_engine, create_sessionmaker
from src.shared.ioc import DatabaseProvider, RepositoriesProvider
from src.shared.security import AuthProvider
from tests.fixtures.client import API_KEY, CALLBACK_SECRET, FakePaymentProviderProvider
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
        FakePaymentProviderProvider(provider if provider is not None else RecordingFakeProvider()),
        CorePaymentProvider(),
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
        repo = SQLAlchemyRefundRepository(session)
        too_young = await repo.list_unresolved(
            statuses=UNRESOLVED_STATUSES, created_before=now - timedelta(minutes=15), limit=10
        )
        overdue = await repo.list_unresolved(
            statuses=UNRESOLVED_STATUSES, created_before=now, limit=10
        )
        settled_only = await repo.list_unresolved(
            statuses=(RefundStatuses.SUCCESS,), created_before=now, limit=10
        )
    await engine.dispose()

    assert too_young == []
    assert settled_only == []
    assert [refund.status for refund in overdue] == [RefundStatuses.CREATED]
    assert overdue[0].payment_id == UUID(payment_id)
    assert await _refunded_amount(payment_id) == Decimal("10.00")


async def _reconcile(provider: PaymentProvider) -> ReconciliationReportDTO:
    """One reconciliation pass with its own engine — the reconciler is a
    separate process in production, so it never shares the API's session."""
    assert TEST_DATABASE_URL is not None
    engine = create_engine(TEST_DATABASE_URL)
    maker = create_sessionmaker(engine)
    async with maker() as session:
        use_case = ReconcileStuckRefundsUseCase(
            SQLAlchemyRefundRepository(session),
            SQLAlchemyPaymentRepository(session),
            provider,
            session,
            stuck_after=timedelta(seconds=0),
            give_up_after=timedelta(days=1),
            batch_size=10,
        )
        report = await use_case()
        await session.commit()
    await engine.dispose()
    return report


async def _stuck_refund(client: AsyncClient, payment_id: str, amount: str) -> None:
    response = await _post_refund(client, payment_id, amount)
    assert response.status_code == 502


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
        repo = SQLAlchemyRefundRepository(session)
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
