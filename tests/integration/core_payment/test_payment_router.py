from uuid import UUID

import pytest
from httpx import AsyncClient
from src.contexts.core_payment.domain.payment import PaymentStatuses
from src.shared.ids import new_uuid
from tests.fixtures.client import API_KEY
from tests.fixtures.payment import FakePaymentRepository, make_payment


@pytest.mark.anyio
async def test_get_payment_returns_200_with_status_when_payment_exists(
    client: AsyncClient, fake_repo: FakePaymentRepository
) -> None:
    payment = make_payment(status=PaymentStatuses.PROCESSING)
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
        f"/payments/{new_uuid()}",
        headers={"X-API-Key": API_KEY},
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Payment not found"}


@pytest.mark.anyio
async def test_get_payment_returns_401_without_api_key(client: AsyncClient) -> None:
    response = await client.get(f"/payments/{new_uuid()}")

    assert response.status_code == 401


@pytest.mark.anyio
async def test_get_payment_returns_401_with_wrong_api_key(client: AsyncClient) -> None:
    response = await client.get(
        f"/payments/{new_uuid()}",
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
