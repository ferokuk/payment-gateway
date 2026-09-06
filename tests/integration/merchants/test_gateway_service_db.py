"""Merchant profile changes cross the real HTTP authentication boundary to payments."""

import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated
from uuid import UUID

import pytest
from cryptography.fernet import Fernet
from dishka import Provider, Scope, make_async_container, provide
from dishka.integrations.fastapi import FastapiProvider, setup_dishka
from fastapi import Depends, FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from src.contexts.core_payment.domain.statuses import PaymentStatuses, RefundStatuses
from src.contexts.core_payment.infrastructure.database.models import (
    IdempotencyKeyModel,
    PaymentModel,
    RefundModel,
)
from src.contexts.core_payment.ioc import CorePaymentProvider, SystemCorePaymentProvider
from src.contexts.core_payment.presentation.routers.callbacks import router as callbacks_router
from src.contexts.core_payment.presentation.routers.payment import router as payment_router
from src.contexts.core_payment.presentation.routers.refund import router as refund_router
from src.contexts.merchants.application.service import MerchantService
from src.contexts.merchants.configuration import MerchantSettings
from src.contexts.merchants.infrastructure.database.models import (
    MerchantAccountModel,
    MerchantModel,
)
from src.contexts.merchants.infrastructure.secrets import SecretVault
from src.contexts.merchants.main import create_app
from src.contexts.merchants.presentation.router import get_service, get_session
from src.shared.config import Settings
from src.shared.database.database import Base
from src.shared.database.engine import create_engine, create_sessionmaker
from src.shared.ioc import DatabaseProvider, RepositoriesProvider, SystemRepositoriesProvider
from src.shared.merchant_client import HTTPMerchantAuthenticator
from src.shared.security import AuthProvider
from tests.fixtures.client import CALLBACK_SECRET, FakePaymentProviderProvider
from tests.fixtures.providers import RecordingFakeProvider

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(TEST_DATABASE_URL is None, reason="TEST_DATABASE_URL is not set"),
]
SERVICE_SECRET = "gateway-service-secret-32-characters-long"
SUPPORT_SECRET = "support-secret-at-least-32-characters-long"
ENCRYPTION_KEY = Fernet.generate_key().decode()
PASSWORD = "merchant-profile-password"
PAYMENT = {"amount": "100.00", "currency": "USD", "provider_id": 1}


class _Config(Provider):
    @provide(scope=Scope.APP)
    def settings(self) -> Settings:
        assert TEST_DATABASE_URL is not None
        return Settings(
            database_url=TEST_DATABASE_URL,
            callback_secret=CALLBACK_SECRET,
            merchant_service_url="http://merchant:8001",
            merchant_service_secret=SERVICE_SECRET,
        )


class _MerchantHTTPTransport(Provider):
    def __init__(self, app: FastAPI) -> None:
        super().__init__()
        self._app = app

    @provide(scope=Scope.APP, override=True)
    async def authenticator(self, settings: Settings) -> AsyncIterator[HTTPMerchantAuthenticator]:
        async with AsyncClient(
            transport=ASGITransport(app=self._app), base_url=settings.merchant_service_url
        ) as client:
            yield HTTPMerchantAuthenticator(client, settings.merchant_service_secret)


@dataclass
class Gateway:
    merchant: AsyncClient
    payment: AsyncClient
    merchant_app: FastAPI
    sessions: async_sessionmaker[AsyncSession]
    provider: RecordingFakeProvider


@dataclass(frozen=True)
class Merchant:
    id: UUID
    api_key_id: UUID
    api_key: str
    access_token: str


@pytest.fixture
async def gateway() -> AsyncIterator[Gateway]:
    assert TEST_DATABASE_URL is not None
    engine = create_engine(TEST_DATABASE_URL)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    merchant_app = create_app(
        MerchantSettings(
            database_url=TEST_DATABASE_URL,
            encryption_key=SecretStr(ENCRYPTION_KEY),
            service_secret=SecretStr(SERVICE_SECRET),
            support_secret=SecretStr(SUPPORT_SECRET),
        )
    )
    payment_app = FastAPI()
    payment_app.include_router(payment_router)
    payment_app.include_router(refund_router)
    payment_app.include_router(callbacks_router)
    provider = RecordingFakeProvider()
    container = make_async_container(
        _Config(),
        DatabaseProvider(),
        RepositoriesProvider(),
        SystemRepositoriesProvider(),
        CorePaymentProvider(),
        SystemCorePaymentProvider(),
        AuthProvider(),
        _MerchantHTTPTransport(merchant_app),
        FakePaymentProviderProvider(provider),
        FastapiProvider(),
    )
    setup_dishka(container, payment_app)
    try:
        async with (
            merchant_app.router.lifespan_context(merchant_app),
            AsyncClient(
                transport=ASGITransport(app=merchant_app), base_url="http://merchant:8001"
            ) as merchant,
            AsyncClient(
                transport=ASGITransport(app=payment_app), base_url="http://payment:8000"
            ) as payment,
        ):
            yield Gateway(merchant, payment, merchant_app, create_sessionmaker(engine), provider)
    finally:
        await container.close()
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
        await engine.dispose()


async def _register(gateway: Gateway, name: str) -> Merchant:
    email = f"{name}@example.com"
    registration = await gateway.merchant.post(
        "/merchants",
        json={
            "email": email,
            "name": name,
            "password": PASSWORD,
            "provider_name": "bank",
            "provider_secret_key": f"provider-secret-{name}",
        },
    )
    assert registration.status_code == 201, registration.text
    profile = registration.json()
    login = await gateway.merchant.post("/auth/token", json={"email": email, "password": PASSWORD})
    assert login.status_code == 200, login.text
    return Merchant(
        UUID(profile["merchant_id"]),
        UUID(profile["api_key_id"]),
        profile["api_key"],
        login.json()["access_token"],
    )


def _headers(api_key: str, idempotency_key: str = "same-idempotency-key") -> dict[str, str]:
    return {"X-API-Key": api_key, "Idempotency-Key": idempotency_key}


async def _succeed_payment(gateway: Gateway, payment_id: str) -> None:
    for status in ("processing", "success"):
        callback = await gateway.payment.post(
            "/callbacks/payments",
            json={"payment_id": payment_id, "status": status},
            headers={"X-Callback-Secret": CALLBACK_SECRET},
        )
        assert callback.status_code == 200, callback.text


async def test_registered_merchants_own_separate_payments_and_refunds(gateway: Gateway) -> None:
    first = await _register(gateway, "first")
    second = await _register(gateway, "second")
    responses = []
    for owner, other in ((first, second), (second, first)):
        response = await gateway.payment.post(
            "/payments",
            json=PAYMENT,
            headers={**_headers(owner.api_key), "X-Merchant-ID": str(other.id)},
        )
        assert response.status_code == 201, response.text
        responses.append(response)
    first_id, second_id = (response.json()["payment_id"] for response in responses)
    assert first_id != second_id
    assert len(gateway.provider.initiated) == 2
    for owner, other, payment_id in ((first, second, first_id), (second, first, second_id)):
        owned = await gateway.payment.get(
            f"/payments/{payment_id}", headers=_headers(owner.api_key)
        )
        assert owned.status_code == 200
        hidden = await gateway.payment.get(
            f"/payments/{payment_id}", headers=_headers(other.api_key)
        )
        assert hidden.status_code == 404
        await _succeed_payment(gateway, payment_id)
        refund = await gateway.payment.post(
            f"/payments/{payment_id}/refunds",
            json={"amount": "10.00"},
            headers=_headers(owner.api_key),
        )
        assert refund.status_code == 201, refund.text
        hidden_refund = await gateway.payment.get(
            f"/refunds/{refund.json()['refund_id']}", headers=_headers(other.api_key)
        )
        assert hidden_refund.status_code == 404
        denied_refund = await gateway.payment.post(
            f"/payments/{payment_id}/refunds",
            json={"amount": "5.00"},
            headers=_headers(other.api_key, "foreign-refund"),
        )
        assert denied_refund.status_code == 404
    # A real profile session is not a payment API key, in either header.
    for headers in (
        {"X-API-Key": first.access_token},
        {"Authorization": f"Bearer {first.access_token}"},
    ):
        assert (
            await gateway.payment.get(f"/payments/{first_id}", headers=headers)
        ).status_code == 401
    async with gateway.sessions() as session:
        owners = {
            payment_id: merchant_id
            for payment_id, merchant_id in (
                await session.execute(select(PaymentModel.id, PaymentModel.merchant_id))
            ).all()
        }
        assert owners == {UUID(first_id): first.id, UUID(second_id): second.id}
        assert await session.scalar(select(func.count()).select_from(IdempotencyKeyModel)) == 2
        assert await session.scalar(select(func.count()).select_from(RefundModel)) == 2


async def test_profile_rotation_preserves_payment_and_idempotency_for_24_hours(
    gateway: Gateway,
) -> None:
    merchant = await _register(gateway, "rotating")
    original = await gateway.payment.post(
        "/payments", json=PAYMENT, headers=_headers(merchant.api_key)
    )
    assert original.status_code == 201, original.text
    payment_id = original.json()["payment_id"]
    before_rotation = datetime.now(UTC)
    rotated = await gateway.merchant.patch(
        "/profile/secrets",
        json={"rotate_api_key": True},
        headers={"Authorization": f"Bearer {merchant.access_token}"},
    )
    after_rotation = datetime.now(UTC)
    assert rotated.status_code == 200, rotated.text
    profile = rotated.json()
    new_key = profile["api_key"]
    assert new_key != merchant.api_key
    assert profile["merchant_id"] == str(merchant.id)
    previous = profile["previous_api_keys"]
    assert len(previous) == 1 and previous[0]["api_key_id"] == str(merchant.api_key_id)
    deadline = datetime.fromisoformat(previous[0]["expires_at"])
    assert before_rotation + timedelta(hours=24) <= deadline
    assert deadline <= after_rotation + timedelta(hours=24)
    for token in (merchant.api_key, new_key):
        replay = await gateway.payment.post("/payments", json=PAYMENT, headers=_headers(token))
        assert replay.status_code == 201
        assert replay.json() == original.json()
        assert replay.headers["Idempotency-Replayed"] == "true"

    def after_grace(session: Annotated[AsyncSession, Depends(get_session)]) -> MerchantService:
        return MerchantService(
            session, SecretVault(ENCRYPTION_KEY), now=lambda: deadline + timedelta(seconds=1)
        )

    gateway.merchant_app.dependency_overrides[get_service] = after_grace
    expired = await gateway.payment.get(
        f"/payments/{payment_id}", headers=_headers(merchant.api_key)
    )
    assert expired.status_code == 401
    active = await gateway.payment.get(f"/payments/{payment_id}", headers=_headers(new_key))
    assert active.status_code == 200
    replay = await gateway.payment.post("/payments", json=PAYMENT, headers=_headers(new_key))
    assert replay.status_code == 201 and replay.json() == original.json()
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert len(gateway.provider.initiated) == 1
    async with gateway.sessions() as session:
        payment = await session.get(PaymentModel, UUID(payment_id))
        assert payment is not None and payment.merchant_id == merchant.id
        assert await session.scalar(select(func.count()).select_from(PaymentModel)) == 1
        assert await session.scalar(select(func.count()).select_from(IdempotencyKeyModel)) == 1


async def test_support_deletion_blocks_next_request_but_callbacks_settle_existing_operations(
    gateway: Gateway,
) -> None:
    merchant = await _register(gateway, "closing")
    first = await gateway.payment.post(
        "/payments", json=PAYMENT, headers=_headers(merchant.api_key)
    )
    assert first.status_code == 201
    first_id = first.json()["payment_id"]
    await _succeed_payment(gateway, first_id)
    refund = await gateway.payment.post(
        f"/payments/{first_id}/refunds",
        json={"amount": "10.00"},
        headers=_headers(merchant.api_key),
    )
    assert refund.status_code == 201, refund.text
    refund_id = refund.json()["refund_id"]
    pending = await gateway.payment.post(
        "/payments", json=PAYMENT, headers=_headers(merchant.api_key, "pending-payment")
    )
    assert pending.status_code == 201
    pending_id = pending.json()["payment_id"]
    deleted = await gateway.merchant.delete(
        "/profile",
        params={"merchant_id": str(merchant.id)},
        headers={"X-Support-Secret": SUPPORT_SECRET},
    )
    assert deleted.status_code == 204, deleted.text
    denied = await gateway.payment.post(
        "/payments", json=PAYMENT, headers=_headers(merchant.api_key, "after-deletion")
    )
    assert denied.status_code == 401
    assert (
        await gateway.payment.get(f"/payments/{first_id}", headers=_headers(merchant.api_key))
    ).status_code == 401
    assert (
        await gateway.merchant.get(
            "/profile", headers={"Authorization": f"Bearer {merchant.access_token}"}
        )
    ).status_code == 401
    await _succeed_payment(gateway, pending_id)
    settled_refund = await gateway.payment.post(
        "/callbacks/refunds",
        json={"refund_id": refund_id, "status": "success"},
        headers={"X-Callback-Secret": CALLBACK_SECRET},
    )
    assert settled_refund.status_code == 200, settled_refund.text
    assert len(gateway.provider.initiated) == 2
    async with gateway.sessions() as session:
        account = await session.get(MerchantAccountModel, merchant.id)
        row = await session.get(MerchantModel, merchant.id)
        assert account is not None and account.email == "closing@example.com"
        assert row is not None and not row.is_active and row.deleted_at is not None
        payments = (await session.scalars(select(PaymentModel))).all()
        assert len(payments) == 2 and all(row.status == PaymentStatuses.SUCCESS for row in payments)
        refunded = await session.get(PaymentModel, UUID(first_id))
        assert refunded is not None and refunded.refunded_amount == Decimal("10.00")
        refund_row = await session.get(RefundModel, UUID(refund_id))
        assert refund_row is not None and refund_row.status == RefundStatuses.SUCCESS
        assert await session.scalar(select(func.count()).select_from(IdempotencyKeyModel)) == 2


async def test_merchant_service_failure_returns_503_without_starting_payment(
    gateway: Gateway,
) -> None:
    merchant = await _register(gateway, "unavailable")

    def unavailable_service() -> MerchantService:
        raise HTTPException(status_code=503, detail="private merchant diagnostics")

    gateway.merchant_app.dependency_overrides[get_service] = unavailable_service
    response = await gateway.payment.post(
        "/payments", json=PAYMENT, headers=_headers(merchant.api_key)
    )
    assert response.status_code == 503
    assert response.json() == {"detail": "Merchant authentication is unavailable"}
    assert gateway.provider.initiated == []
    async with gateway.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(PaymentModel)) == 0
        assert await session.scalar(select(func.count()).select_from(IdempotencyKeyModel)) == 0
