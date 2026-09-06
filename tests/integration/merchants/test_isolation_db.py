"""Tenant boundaries against PostgreSQL, including shared identity maps and races."""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import anyio
import pytest
from dishka import Provider, Scope, make_async_container, provide
from dishka.integrations.fastapi import FastapiProvider, setup_dishka
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from src.contexts.core_payment.application.use_cases.reconcile_stuck_refunds import (
    UNRESOLVED_STATUSES,
    ReconcileStuckRefundsUseCase,
)
from src.contexts.core_payment.domain.exceptions import PaymentNotFoundError, RefundNotFoundError
from src.contexts.core_payment.domain.payment import Payment
from src.contexts.core_payment.domain.refund import Refund
from src.contexts.core_payment.domain.statuses import (
    PaymentStatuses,
    RefundFailureReasons,
    RefundStatuses,
)
from src.contexts.core_payment.infrastructure.database.models import (
    IdempotencyKeyModel,
    PaymentModel,
    RefundIdempotencyKeyModel,
    RefundModel,
)
from src.contexts.core_payment.infrastructure.database.repositories import (
    IdempotencyRecord,
    RefundIdempotencyRecord,
    RefundReconciliationCandidate,
    SQLAlchemyIdempotencyKeyRepository,
    SQLAlchemyPaymentRepository,
    SQLAlchemyRefundIdempotencyKeyRepository,
    SQLAlchemyRefundRepository,
)
from src.contexts.core_payment.infrastructure.providers.base import (
    ProviderInitiationError,
    RefundProviderState,
    RefundProviderStatus,
)
from src.contexts.core_payment.ioc import CorePaymentProvider, SystemCorePaymentProvider
from src.contexts.core_payment.presentation.routers.callbacks import router as callbacks_router
from src.contexts.core_payment.presentation.routers.payment import router as payment_router
from src.contexts.core_payment.presentation.routers.refund import router as refund_router
from src.contexts.merchants.domain.api_key import IssuedAPIKey
from src.contexts.merchants.infrastructure.database.repositories import SQLAlchemyMerchantRepository
from src.shared.config import Settings
from src.shared.database.database import Base
from src.shared.database.engine import create_engine, create_sessionmaker
from src.shared.ids import new_uuid
from src.shared.ioc import DatabaseProvider, RepositoriesProvider, SystemRepositoriesProvider
from tests.fixtures.client import CALLBACK_SECRET, FakePaymentProviderProvider
from tests.fixtures.merchants import LocalMerchantAuthProvider
from tests.fixtures.providers import RecordingFakeProvider

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(TEST_DATABASE_URL is None, reason="TEST_DATABASE_URL is not set"),
]


@dataclass(frozen=True)
class TenantDatabase:
    maker: async_sessionmaker[AsyncSession]
    first: IssuedAPIKey
    second: IssuedAPIKey


class _Config(Provider):
    @provide(scope=Scope.APP)
    def settings(self) -> Settings:
        assert TEST_DATABASE_URL is not None
        return Settings(database_url=TEST_DATABASE_URL, callback_secret=CALLBACK_SECRET)


@pytest.fixture
async def tenant_db() -> AsyncIterator[TenantDatabase]:
    assert TEST_DATABASE_URL is not None
    engine = create_engine(TEST_DATABASE_URL)
    maker = create_sessionmaker(engine)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    try:
        async with maker() as session:
            merchants = SQLAlchemyMerchantRepository(session)
            first = await merchants.create("First merchant")
            second = await merchants.create("Second merchant")
            first_key = await merchants.issue_key(first.id, "First credential")
            second_key = await merchants.issue_key(second.id, "Second credential")
            await session.commit()
        yield TenantDatabase(maker, first_key, second_key)
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
        await engine.dispose()


@asynccontextmanager
async def _client(provider: RecordingFakeProvider) -> AsyncIterator[AsyncClient]:
    app = FastAPI()
    app.include_router(payment_router)
    app.include_router(refund_router)
    app.include_router(callbacks_router)
    container = make_async_container(
        _Config(),
        DatabaseProvider(),
        RepositoriesProvider(),
        SystemRepositoriesProvider(),
        CorePaymentProvider(),
        SystemCorePaymentProvider(),
        LocalMerchantAuthProvider(),
        FakePaymentProviderProvider(provider),
        FastapiProvider(),
    )
    setup_dishka(container, app)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield client
    finally:
        await container.close()


def _headers(token: str, key: str | None = None) -> dict[str, str]:
    headers = {"X-API-Key": token}
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


async def _succeed(client: AsyncClient, payment_id: str) -> None:
    for status in ("processing", "success"):
        response = await client.post(
            "/callbacks/payments",
            json={"payment_id": payment_id, "status": status},
            headers={"X-Callback-Secret": CALLBACK_SECRET},
        )
        assert response.status_code == 200


async def _payment(client: AsyncClient, key: IssuedAPIKey) -> str:
    response = await client.post(
        "/payments",
        json={"amount": "100.00", "currency": "USD", "provider_id": 1},
        headers=_headers(key.token),
    )
    assert response.status_code == 201
    payment_id = str(response.json()["payment_id"])
    await _succeed(client, payment_id)
    return payment_id


async def _seed_financial(db: TenantDatabase, key: IssuedAPIKey) -> tuple[Payment, Refund]:
    owner = key.key.merchant_id
    payment = Payment(
        id=new_uuid(),
        merchant_id=owner,
        provider_id=1,
        status=PaymentStatuses.SUCCESS,
        amount=Decimal("100"),
        currency="USD",
        created_at=datetime.now(UTC),
        refunded_amount=Decimal("40"),
    )
    refund = Refund(
        id=new_uuid(),
        merchant_id=owner,
        payment_id=payment.id,
        amount=Decimal("40"),
        status=RefundStatuses.PENDING,
        created_at=datetime.now(UTC) - timedelta(hours=1),
    )
    async with db.maker() as session:
        await SQLAlchemyPaymentRepository(session, owner).add(payment)
        await session.flush()
        await SQLAlchemyRefundRepository(session, owner).add(refund)
        await session.flush()
        await SQLAlchemyIdempotencyKeyRepository(session, owner).add(
            IdempotencyRecord(
                merchant_id=owner,
                key=str(owner),
                request_hash="a" * 64,
                payment_id=payment.id,
                response_body={"owner": str(owner)},
            )
        )
        await SQLAlchemyRefundIdempotencyKeyRepository(session, owner).add(
            RefundIdempotencyRecord(
                merchant_id=owner,
                key=str(owner),
                request_hash="b" * 64,
                refund_id=refund.id,
                response_body={"owner": str(owner)},
            )
        )
        await session.commit()
    return payment, refund


async def test_http_idor_and_owner_spoofing(tenant_db: TenantDatabase) -> None:
    provider = RecordingFakeProvider()
    async with _client(provider) as client:
        own_payment = await _payment(client, tenant_db.first)
        other_payment = await _payment(client, tenant_db.second)
        refunds = []
        for key, payment_id in ((tenant_db.first, own_payment), (tenant_db.second, other_payment)):
            response = await client.post(
                f"/payments/{payment_id}/refunds",
                json={"amount": "40.00"},
                headers=_headers(key.token, "known-refund-key"),
            )
            assert response.status_code == 201
            refunds.append(str(response.json()["refund_id"]))
        before_calls = len(provider.initiated_refunds)
        for payment_id, refund_id in (
            (other_payment, refunds[1]),
            (str(new_uuid()), str(new_uuid())),
        ):
            for path in (
                f"/payments/{payment_id}",
                f"/payments/{payment_id}/refunds",
                f"/refunds/{refund_id}",
            ):
                response = await client.get(path, headers=_headers(tenant_db.first.token))
                assert response.status_code == 404
            response = await client.post(
                f"/payments/{payment_id}/refunds",
                json={"amount": "40.00"},
                headers=_headers(tenant_db.first.token, "known-refund-key"),
            )
            assert response.status_code == 404
        assert len(provider.initiated_refunds) == before_calls
        spoofed = await client.post(
            f"/payments?merchant_id={tenant_db.second.key.merchant_id}",
            json={
                "amount": "10.00",
                "currency": "USD",
                "provider_id": 1,
                "merchant_id": str(tenant_db.second.key.merchant_id),
                "metadata": {"merchant_id": str(tenant_db.second.key.merchant_id)},
            },
            headers={
                **_headers(tenant_db.first.token),
                "X-Merchant-Id": str(tenant_db.second.key.merchant_id),
            },
        )
        assert spoofed.status_code == 201
        spoofed_id = UUID(spoofed.json()["payment_id"])
        assert (
            await client.get(f"/payments/{spoofed_id}", headers=_headers(tenant_db.second.token))
        ).status_code == 404
    async with tenant_db.maker() as session:
        assert await session.scalar(select(func.count()).select_from(RefundModel)) == 2
        payment = await session.get(PaymentModel, UUID(other_payment))
        assert payment is not None and payment.refunded_amount == Decimal("40")
        assert (
            await session.scalar(
                select(PaymentModel.merchant_id).where(PaymentModel.id == spoofed_id)
            )
            == tenant_db.first.key.merchant_id
        )


async def test_concurrent_namespaces_and_key_rotation(tenant_db: TenantDatabase) -> None:
    replies: dict[str, list[Response]] = {"first": [], "second": []}
    async with _client(RecordingFakeProvider()) as client:

        async def create(name: str, token: str, amount: str) -> None:
            replies[name].append(
                await client.post(
                    "/payments",
                    json={"amount": amount, "currency": "USD", "provider_id": 1},
                    headers=_headers(token, "shared-key"),
                )
            )

        async with anyio.create_task_group() as tasks:
            for _ in range(2):
                tasks.start_soon(create, "first", tenant_db.first.token, "100.00")
                tasks.start_soon(create, "second", tenant_db.second.token, "200.00")
        for responses in replies.values():
            assert all(response.status_code == 201 for response in responses)
            assert responses[0].json() == responses[1].json()
        first_id = str(replies["first"][0].json()["payment_id"])
        second_id = str(replies["second"][0].json()["payment_id"])
        assert first_id != second_id
        async with tenant_db.maker() as session:
            merchants = SQLAlchemyMerchantRepository(session)
            rotated = await merchants.issue_key(tenant_db.first.key.merchant_id, "Rotation")
            await merchants.revoke_key(tenant_db.first.key.id)
            await session.commit()
        replay = await client.post(
            "/payments",
            json={"amount": "100.00", "currency": "USD", "provider_id": 1},
            headers=_headers(rotated.token, "shared-key"),
        )
        assert replay.status_code == 201 and replay.json() == replies["first"][0].json()
        assert replay.headers["Idempotency-Replayed"] == "true"
        assert (
            await client.get(f"/payments/{first_id}", headers=_headers(tenant_db.first.token))
        ).status_code == 401
        for payment_id in (first_id, second_id):
            await _succeed(client, payment_id)
        refund_replies: dict[str, list[Response]] = {"first": [], "second": []}

        async def create_refund(name: str, payment_id: str, token: str, amount: str) -> None:
            refund_replies[name].append(
                await client.post(
                    f"/payments/{payment_id}/refunds",
                    json={"amount": amount},
                    headers=_headers(token, "shared-key"),
                )
            )

        async with anyio.create_task_group() as tasks:
            for _ in range(2):
                tasks.start_soon(create_refund, "first", first_id, rotated.token, "10.00")
                tasks.start_soon(
                    create_refund, "second", second_id, tenant_db.second.token, "20.00"
                )
        for responses in refund_replies.values():
            assert all(response.status_code == 201 for response in responses)
            assert responses[0].json() == responses[1].json()
        assert (
            refund_replies["first"][0].json()["refund_id"]
            != refund_replies["second"][0].json()["refund_id"]
        )
        async with tenant_db.maker() as session:
            replacement = await SQLAlchemyMerchantRepository(session).issue_key(
                tenant_db.first.key.merchant_id,
                "Refund rotation",
            )
            await session.commit()
        replayed_refund = await client.post(
            f"/payments/{first_id}/refunds",
            json={"amount": "10.00"},
            headers=_headers(replacement.token, "shared-key"),
        )
        assert replayed_refund.json() == refund_replies["first"][0].json()
        assert replayed_refund.headers["Idempotency-Replayed"] == "true"
    async with tenant_db.maker() as session:
        assert await session.scalar(select(func.count()).select_from(PaymentModel)) == 2
        assert await session.scalar(select(func.count()).select_from(IdempotencyKeyModel)) == 2
        assert (
            await session.scalar(select(func.count()).select_from(RefundIdempotencyKeyModel)) == 2
        )
        assert sorted((await session.scalars(select(PaymentModel.refunded_amount))).all()) == [
            Decimal("10"),
            Decimal("20"),
        ]


async def test_repository_scopes_include_fallbacks_and_cached_objects(
    tenant_db: TenantDatabase,
) -> None:
    own_payment, own_refund = await _seed_financial(tenant_db, tenant_db.first)
    other_payment, other_refund = await _seed_financial(tenant_db, tenant_db.second)
    owner = tenant_db.first.key.merchant_id
    async with tenant_db.maker() as session:
        cached_payment = await session.get(PaymentModel, other_payment.id)
        cached_refund = await session.get(RefundModel, other_refund.id)
        assert cached_payment is not None and cached_refund is not None
        payments = SQLAlchemyPaymentRepository(session, owner)
        refunds = SQLAlchemyRefundRepository(session, owner)
        assert await payments.get_by_id(other_payment.id) is None
        assert await refunds.get_by_id(other_refund.id) is None
        assert await refunds.list_by_payment_id(other_payment.id) == []
        for action in (payments.reserve_refund_amount, payments.release_refund_amount):
            with pytest.raises(PaymentNotFoundError):
                await action(other_payment.id, Decimal("1"))
        with pytest.raises(ValueError, match="merchant scope"):
            await payments.add(other_payment)
        with pytest.raises(ValueError, match="merchant scope"):
            await refunds.add(other_refund)
        with pytest.raises(ValueError, match="merchant scope"):
            await payments.update(other_payment, expected_status=PaymentStatuses.SUCCESS)
        with pytest.raises(ValueError, match="merchant scope"):
            await refunds.update(other_refund, expected_status=RefundStatuses.PENDING)
        with pytest.raises(PaymentNotFoundError):
            await payments.update(
                other_payment.model_copy(update={"merchant_id": owner}),
                expected_status=PaymentStatuses.SUCCESS,
            )
        forged_refund = other_refund.model_copy(update={"merchant_id": owner})
        with pytest.raises(RefundNotFoundError):
            await refunds.update(forged_refund, expected_status=RefundStatuses.PENDING)
        assert not await refunds.schedule_reconciliation(
            RefundReconciliationCandidate(forged_refund, 0),
            next_check_at=datetime.now(UTC),
        )
        with pytest.raises(ValueError, match="merchant scope"):
            await refunds.schedule_reconciliation(
                RefundReconciliationCandidate(other_refund, 0),
                next_check_at=datetime.now(UTC),
            )
        candidates = await refunds.list_unresolved(
            statuses=UNRESOLVED_STATUSES,
            created_before=datetime.now(UTC),
            due_before=datetime.now(UTC),
            limit=10,
        )
        assert [candidate.refund.id for candidate in candidates] == [own_refund.id]
        payment_keys = SQLAlchemyIdempotencyKeyRepository(session, owner)
        refund_keys = SQLAlchemyRefundIdempotencyKeyRepository(session, owner)
        foreign_key = str(tenant_db.second.key.merchant_id)
        for repository in (payment_keys, refund_keys):
            assert await repository.get(foreign_key) is None
            with pytest.raises(LookupError):
                await repository.set_response(foreign_key, {"changed": True})
        with pytest.raises(ValueError, match="merchant scope"):
            await payment_keys.add(
                IdempotencyRecord(
                    merchant_id=other_payment.merchant_id,
                    key="foreign",
                    request_hash="a" * 64,
                    payment_id=other_payment.id,
                )
            )
        with pytest.raises(ValueError, match="merchant scope"):
            await refund_keys.add(
                RefundIdempotencyRecord(
                    merchant_id=other_refund.merchant_id,
                    key="foreign",
                    request_hash="b" * 64,
                    refund_id=other_refund.id,
                )
            )
        await payments.reserve_refund_amount(own_payment.id, Decimal("1"))
        await payments.release_refund_amount(own_payment.id, Decimal("1"))
        await session.commit()
    async with tenant_db.maker() as session:
        unchanged = await session.get(PaymentModel, other_payment.id)
        assert unchanged is not None and unchanged.refunded_amount == Decimal("40")
        unchanged_refund = await session.get(RefundModel, other_refund.id)
        assert unchanged_refund is not None and unchanged_refund.reconciliation_attempts == 0
        foreign_identity = (other_payment.merchant_id, str(other_payment.merchant_id))
        for record in (
            await session.get(IdempotencyKeyModel, foreign_identity),
            await session.get(RefundIdempotencyKeyModel, foreign_identity),
        ):
            assert record is not None and record.response_body == {
                "owner": str(other_payment.merchant_id)
            }


@pytest.mark.parametrize("relationship", ["refund", "payment_key", "refund_key"])
async def test_database_rejects_cross_owner_relationships(
    tenant_db: TenantDatabase, relationship: str
) -> None:
    other_payment, other_refund = await _seed_financial(tenant_db, tenant_db.second)
    owner = tenant_db.first.key.merchant_id
    async with tenant_db.maker() as session:
        with pytest.raises(IntegrityError):
            async with session.begin_nested():
                if relationship == "refund":
                    session.add(
                        RefundModel(
                            id=new_uuid(),
                            merchant_id=owner,
                            payment_id=other_payment.id,
                            amount=Decimal("1"),
                            status=RefundStatuses.PENDING,
                        )
                    )
                elif relationship == "payment_key":
                    session.add(
                        IdempotencyKeyModel(
                            merchant_id=owner,
                            key="forged",
                            payment_id=other_payment.id,
                            request_hash="a" * 64,
                        )
                    )
                else:
                    session.add(
                        RefundIdempotencyKeyModel(
                            merchant_id=owner,
                            key="forged",
                            refund_id=other_refund.id,
                            request_hash="b" * 64,
                        )
                    )
                await session.flush()
        assert await session.scalar(select(func.count()).select_from(RefundModel)) == 1


async def test_service_settlement_includes_disabled_merchants(tenant_db: TenantDatabase) -> None:
    rows = [await _seed_financial(tenant_db, key) for key in (tenant_db.first, tenant_db.second)]
    async with tenant_db.maker() as session:
        await SQLAlchemyMerchantRepository(session).set_active(
            tenant_db.second.key.merchant_id, active=False
        )
        await session.commit()
    async with _client(RecordingFakeProvider()) as client:
        for _, refund in rows:
            denied = await client.post(
                "/callbacks/refunds",
                json={"refund_id": str(refund.id), "status": "error", "error_message": "unknown"},
                headers={"X-Callback-Secret": tenant_db.first.token},
            )
            assert denied.status_code == 401
            response = await client.post(
                "/callbacks/refunds",
                json={"refund_id": str(refund.id), "status": "error", "error_message": "unknown"},
                headers={"X-Callback-Secret": CALLBACK_SECRET},
            )
            assert response.status_code == 200
        assert (
            await client.get(f"/payments/{rows[1][0].id}", headers=_headers(tenant_db.second.token))
        ).status_code == 401
        assert (
            await client.get(f"/payments/{rows[0][0].id}", headers=_headers(CALLBACK_SECRET))
        ).status_code == 401
    provider = RecordingFakeProvider(
        refund_status=RefundProviderStatus(
            RefundProviderState.FAILED,
            failure_reason=RefundFailureReasons.TIMEOUT,
        )
    )
    # This worker container has neither Request nor merchant authentication.
    container = make_async_container(
        _Config(),
        DatabaseProvider(),
        SystemRepositoriesProvider(),
        SystemCorePaymentProvider(),
        FakePaymentProviderProvider(provider),
    )
    try:
        async with container() as scope:
            report = await (await scope.get(ReconcileStuckRefundsUseCase))()
    finally:
        await container.close()
    assert report.closed == 2
    assert {refund.merchant_id for refund in provider.status_queries} == {
        tenant_db.first.key.merchant_id,
        tenant_db.second.key.merchant_id,
    }
    async with tenant_db.maker() as session:
        assert list((await session.scalars(select(PaymentModel.refunded_amount))).all()) == [
            Decimal("0"),
            Decimal("0"),
        ]
        assert list((await session.scalars(select(RefundModel.status))).all()) == [
            RefundStatuses.FAILED,
            RefundStatuses.FAILED,
        ]


async def test_missing_payment_snapshot_recovers_only_its_tenant(tenant_db: TenantDatabase) -> None:
    payload = {"amount": "100.00", "currency": "USD", "provider_id": 1}
    async with _client(RecordingFakeProvider(error=ProviderInitiationError("offline"))) as client:
        failed = await client.post(
            "/payments", json=payload, headers=_headers(tenant_db.first.token, "resume")
        )
        assert failed.status_code == 502
    async with tenant_db.maker() as session:
        original_id = await session.scalar(
            select(PaymentModel.id).where(
                PaymentModel.merchant_id == tenant_db.first.key.merchant_id
            )
        )
    async with _client(RecordingFakeProvider()) as client:
        second = await client.post(
            "/payments", json=payload, headers=_headers(tenant_db.second.token, "resume")
        )
        recovered = await client.post(
            "/payments", json=payload, headers=_headers(tenant_db.first.token, "resume")
        )
        assert second.status_code == recovered.status_code == 201
        assert recovered.json()["payment_id"] == str(original_id)
        assert second.json()["payment_id"] != recovered.json()["payment_id"]
        assert (
            await client.get(f"/payments/{original_id}", headers=_headers(tenant_db.second.token))
        ).status_code == 404


async def test_missing_refund_snapshot_recovers_without_foreign_replay(
    tenant_db: TenantDatabase,
) -> None:
    async with _client(RecordingFakeProvider()) as client:
        first_id = await _payment(client, tenant_db.first)
        second_id = await _payment(client, tenant_db.second)
    async with _client(
        RecordingFakeProvider(refund_error=ProviderInitiationError("offline"))
    ) as client:
        failed = await client.post(
            f"/payments/{first_id}/refunds",
            json={"amount": "10.00"},
            headers=_headers(tenant_db.first.token, "resume-refund"),
        )
        assert failed.status_code == 502
    async with tenant_db.maker() as session:
        original_id = await session.scalar(
            select(RefundModel.id).where(
                RefundModel.merchant_id == tenant_db.first.key.merchant_id,
            )
        )
    async with _client(RecordingFakeProvider()) as client:
        second = await client.post(
            f"/payments/{second_id}/refunds",
            json={"amount": "20.00"},
            headers=_headers(tenant_db.second.token, "resume-refund"),
        )
        recovered = await client.post(
            f"/payments/{first_id}/refunds",
            json={"amount": "10.00"},
            headers=_headers(tenant_db.first.token, "resume-refund"),
        )
        assert second.status_code == recovered.status_code == 201
        assert recovered.json()["refund_id"] == str(original_id)
        assert second.json()["refund_id"] != recovered.json()["refund_id"]
        settled = await client.post(
            "/callbacks/refunds",
            json={"refund_id": str(original_id), "status": "success"},
            headers={"X-Callback-Secret": CALLBACK_SECRET},
        )
        assert settled.status_code == 200
        replayed = await client.post(
            f"/payments/{first_id}/refunds",
            json={"amount": "10.00"},
            headers=_headers(tenant_db.first.token, "resume-refund"),
        )
        assert replayed.json() == recovered.json()
        assert replayed.headers["Idempotency-Replayed"] == "true"
    async with tenant_db.maker() as session:
        assert sorted((await session.scalars(select(PaymentModel.refunded_amount))).all()) == [
            Decimal("10"),
            Decimal("20"),
        ]
        assert await session.scalar(select(func.count()).select_from(RefundModel)) == 2
