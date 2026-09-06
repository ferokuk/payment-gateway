"""End-to-end with the auto fake: the provider posts real HTTP callbacks back
into the same ASGI app, exactly like the compose setup with FAKE_PROVIDER_MODE=auto.

The fake session commits are no-ops over dicts, so unlike a real DB there is no
commit-visibility race; delay_seconds only has to let the request finish first.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal

import pytest
from dishka import make_async_container
from dishka.integrations.fastapi import FastapiProvider, setup_dishka
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from src.contexts.core_payment.infrastructure.providers.fake_auto import (
    AutoCallbackFakePaymentProvider,
)
from src.contexts.core_payment.ioc import CorePaymentProvider, SystemCorePaymentProvider
from src.contexts.core_payment.presentation.routers.callbacks import (
    router as callbacks_router,
)
from src.contexts.core_payment.presentation.routers.payment import router as payment_router
from src.contexts.core_payment.presentation.routers.refund import router as refund_router
from tests.fixtures.client import (
    API_KEY,
    CALLBACK_SECRET,
    FakeAuthProvider,
    FakeConfigProvider,
    FakePaymentProviderProvider,
    FakeRepositoriesProvider,
    FakeSessionProvider,
)
from tests.fixtures.idempotency import FakeIdempotencyKeyRepository
from tests.fixtures.payment import FakePaymentRepository
from tests.fixtures.refund import FakeRefundIdempotencyKeyRepository, FakeRefundRepository
from tests.fixtures.session import FakeSession


@asynccontextmanager
async def _auto_client(
    fake_repo: FakePaymentRepository,
    fake_key_repo: FakeIdempotencyKeyRepository,
    fake_refund_repo: FakeRefundRepository,
    fake_refund_key_repo: FakeRefundIdempotencyKeyRepository,
    fake_session: FakeSession,
) -> AsyncIterator[tuple[AsyncClient, AutoCallbackFakePaymentProvider]]:
    app = FastAPI()
    app.include_router(payment_router)
    app.include_router(callbacks_router)
    app.include_router(refund_router)
    provider_client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    provider = AutoCallbackFakePaymentProvider(
        http_client=provider_client,
        callback_url="http://test/callbacks/payments",
        refund_callback_url="http://test/callbacks/refunds",
        callback_secret=CALLBACK_SECRET,
        delay_seconds=0.01,
    )
    container = make_async_container(
        FakeConfigProvider(),
        FakeAuthProvider(),
        FakeRepositoriesProvider(fake_repo, fake_key_repo, fake_refund_repo, fake_refund_key_repo),
        FakeSessionProvider(fake_session),
        FakePaymentProviderProvider(provider),
        CorePaymentProvider(),
        SystemCorePaymentProvider(),
        FastapiProvider(),
    )
    setup_dishka(container, app)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac, provider
    await provider.aclose()
    await provider_client.aclose()
    await container.close()


async def _wait_callbacks(provider: AutoCallbackFakePaymentProvider) -> None:
    tasks = set(provider._tasks)
    if tasks:
        # Hang guard only; generous so a loaded CI runner does not flake.
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)


async def _create_success_payment(
    client: AsyncClient, provider: AutoCallbackFakePaymentProvider
) -> str:
    response = await client.post(
        "/payments",
        json={"amount": "100.50", "currency": "USD", "provider_id": 1},
        headers={"X-API-Key": API_KEY},
    )
    assert response.status_code == 201
    await _wait_callbacks(provider)
    payment_id = str(response.json()["payment_id"])
    status = await client.get(f"/payments/{payment_id}", headers={"X-API-Key": API_KEY})
    assert status.json()["status"] == "success"
    return payment_id


async def _create_refund(
    client: AsyncClient,
    provider: AutoCallbackFakePaymentProvider,
    payment_id: str,
    scenario: str,
) -> str:
    response = await client.post(
        f"/payments/{payment_id}/refunds",
        json={"amount": "40.00", "metadata": {"fake_scenario": scenario}},
        headers={"X-API-Key": API_KEY},
    )
    assert response.status_code == 201
    await _wait_callbacks(provider)
    return str(response.json()["refund_id"])


async def _refund_status(client: AsyncClient, refund_id: str) -> str:
    response = await client.get(f"/refunds/{refund_id}", headers={"X-API-Key": API_KEY})
    assert response.status_code == 200
    return str(response.json()["status"])


async def _refunded_amount(client: AsyncClient, payment_id: str) -> Decimal:
    response = await client.get(f"/payments/{payment_id}", headers={"X-API-Key": API_KEY})
    return Decimal(str(response.json()["refunded_amount"]))


@pytest.mark.anyio
async def test_auto_e2e_refund_success(
    fake_repo: FakePaymentRepository,
    fake_key_repo: FakeIdempotencyKeyRepository,
    fake_refund_repo: FakeRefundRepository,
    fake_refund_key_repo: FakeRefundIdempotencyKeyRepository,
    fake_session: FakeSession,
) -> None:
    async with _auto_client(
        fake_repo, fake_key_repo, fake_refund_repo, fake_refund_key_repo, fake_session
    ) as (client, provider):
        payment_id = await _create_success_payment(client, provider)
        refund_id = await _create_refund(client, provider, payment_id, "success")

        assert await _refund_status(client, refund_id) == "success"
        assert await _refunded_amount(client, payment_id) == Decimal("40.00")


@pytest.mark.parametrize(
    "scenario", ["card_unavailable", "insufficient_merchant_balance", "timeout"]
)
@pytest.mark.anyio
async def test_auto_e2e_refund_failed_releases(
    fake_repo: FakePaymentRepository,
    fake_key_repo: FakeIdempotencyKeyRepository,
    fake_refund_repo: FakeRefundRepository,
    fake_refund_key_repo: FakeRefundIdempotencyKeyRepository,
    fake_session: FakeSession,
    scenario: str,
) -> None:
    async with _auto_client(
        fake_repo, fake_key_repo, fake_refund_repo, fake_refund_key_repo, fake_session
    ) as (client, provider):
        payment_id = await _create_success_payment(client, provider)
        refund_id = await _create_refund(client, provider, payment_id, scenario)

        assert await _refund_status(client, refund_id) == "failed"
        assert await _refunded_amount(client, payment_id) == Decimal("0")


@pytest.mark.anyio
async def test_auto_e2e_refund_error_holds(
    fake_repo: FakePaymentRepository,
    fake_key_repo: FakeIdempotencyKeyRepository,
    fake_refund_repo: FakeRefundRepository,
    fake_refund_key_repo: FakeRefundIdempotencyKeyRepository,
    fake_session: FakeSession,
) -> None:
    async with _auto_client(
        fake_repo, fake_key_repo, fake_refund_repo, fake_refund_key_repo, fake_session
    ) as (client, provider):
        payment_id = await _create_success_payment(client, provider)
        refund_id = await _create_refund(client, provider, payment_id, "error")

        assert await _refund_status(client, refund_id) == "error"
        assert await _refunded_amount(client, payment_id) == Decimal("40.00")
