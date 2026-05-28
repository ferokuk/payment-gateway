from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from dishka import Provider, Scope, make_async_container, provide
from dishka.integrations.fastapi import FastapiProvider, setup_dishka
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from src.contexts.core_payment.domain.payment import Payment, PaymentStatuses
from src.contexts.core_payment.infrastructure.database.repositories import (
    SQLAlchemyPaymentRepository,
)
from src.contexts.core_payment.ioc import CorePaymentProvider
from src.contexts.core_payment.presentation.routers.payment import router as payment_router
from src.shared.config import Settings
from src.shared.security import AuthProvider

API_KEY = "test-api-key"


class FakePaymentRepository:
    def __init__(self) -> None:
        self._payments: dict[UUID, Payment] = {}

    async def add(self, payment: Payment) -> None:
        self._payments[payment.id] = payment

    async def get_by_id(self, payment_id: UUID) -> Payment | None:
        return self._payments.get(payment_id)


class FakeConfigProvider(Provider):
    scope = Scope.APP

    @provide
    def get_settings(self) -> Settings:
        return Settings(database_url="sqlite+aiosqlite:///:memory:", api_key=API_KEY)


class FakeRepositoriesProvider(Provider):
    scope = Scope.REQUEST

    def __init__(self, repo: FakePaymentRepository) -> None:
        super().__init__()
        self._repo = repo

    @provide
    def get_payment_repository(self) -> SQLAlchemyPaymentRepository:
        return self._repo  # type: ignore[return-value]


@pytest.fixture
def fake_repo() -> FakePaymentRepository:
    return FakePaymentRepository()


@pytest.fixture
async def client(fake_repo: FakePaymentRepository) -> AsyncIterator[AsyncClient]:
    app = FastAPI()
    app.include_router(payment_router)
    container = make_async_container(
        FakeConfigProvider(),
        AuthProvider(),
        FakeRepositoriesProvider(fake_repo),
        CorePaymentProvider(),
        FastapiProvider(),
    )
    setup_dishka(container, app)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    await container.close()


def _make_payment(status: PaymentStatuses = PaymentStatuses.PENDING) -> Payment:
    return Payment(
        id=uuid4(),
        provider_id=1,
        status=status,
        amount=Decimal("100.00"),
        currency="USD",
        created_at=datetime.now(UTC),
    )


@pytest.mark.anyio
async def test_get_payment_returns_200_with_status_when_payment_exists(
    client: AsyncClient, fake_repo: FakePaymentRepository
) -> None:
    payment = _make_payment(status=PaymentStatuses.PROCESSING)
    await fake_repo.add(payment)

    response = await client.get(
        f"/payments/{payment.id}",
        headers={"X-API-Key": API_KEY},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["payment_id"] == str(payment.id)
    assert body["status"] == "processing"


@pytest.mark.anyio
async def test_get_payment_returns_404_when_payment_missing(client: AsyncClient) -> None:
    response = await client.get(
        f"/payments/{uuid4()}",
        headers={"X-API-Key": API_KEY},
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Payment not found"}


@pytest.mark.anyio
async def test_get_payment_returns_401_without_api_key(client: AsyncClient) -> None:
    response = await client.get(f"/payment/{uuid4()}")

    assert response.status_code == 401


@pytest.mark.anyio
async def test_get_payment_returns_401_with_wrong_api_key(client: AsyncClient) -> None:
    response = await client.get(
        f"/payments/{uuid4()}",
        headers={"X-API-Key": "wrong-key"},
    )

    assert response.status_code == 401


def _valid_create_payload() -> dict[str, object]:
    return {
        "amount": "100.50",
        "currency": "USD",
        "provider_id": 1,
        "metadata": {"order_id": "abc-123"},
    }


@pytest.mark.anyio
async def test_create_payment_returns_201_and_persists_payment(
    client: AsyncClient, fake_repo: FakePaymentRepository
) -> None:
    response = await client.post(
        "/payments",
        json=_valid_create_payload(),
        headers={"X-API-Key": API_KEY},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == PaymentStatuses.CREATED.value
    assert body["amount"] == "100.50"
    assert body["currency"] == "USD"
    assert "payment_id" in body
    assert "created_at" in body

    stored = await fake_repo.get_by_id(UUID(body["payment_id"]))
    assert stored is not None
    assert stored.provider_id == 1
    assert stored.metadata == {"order_id": "abc-123"}


@pytest.mark.anyio
async def test_create_payment_accepts_payload_without_metadata(client: AsyncClient) -> None:
    payload = _valid_create_payload()
    del payload["metadata"]

    response = await client.post(
        "/payments",
        json=payload,
        headers={"X-API-Key": API_KEY},
    )

    assert response.status_code == 201


@pytest.mark.anyio
async def test_create_payment_returns_401_without_api_key(client: AsyncClient) -> None:
    response = await client.post("/payments", json=_valid_create_payload())

    assert response.status_code == 401


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("amount", "0"),
        ("amount", "-1"),
        ("currency", "us"),
        ("currency", "USDX"),
        ("currency", "usd"),
        ("provider_id", 0),
        ("provider_id", -5),
    ],
)
@pytest.mark.anyio
async def test_create_payment_returns_422_on_invalid_field(
    client: AsyncClient, field: str, value: object
) -> None:
    payload = _valid_create_payload()
    payload[field] = value

    response = await client.post(
        "/payments",
        json=payload,
        headers={"X-API-Key": API_KEY},
    )

    assert response.status_code == 422
