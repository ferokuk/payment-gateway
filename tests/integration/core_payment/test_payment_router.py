from decimal import Decimal
from uuid import UUID

import pytest
from httpx import AsyncClient
from src.contexts.core_payment.domain.statuses import PaymentStatuses
from src.contexts.core_payment.infrastructure.providers.base import ProviderInitiationError
from src.shared.ids import new_uuid
from tests.fixtures.client import API_KEY, make_client
from tests.fixtures.idempotency import FakeIdempotencyKeyRepository
from tests.fixtures.payment import FakePaymentRepository, make_payment
from tests.fixtures.providers import RecordingFakeProvider
from tests.fixtures.refund import FakeRefundIdempotencyKeyRepository, FakeRefundRepository
from tests.fixtures.session import FakeSession


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
    assert Decimal(body["refunded_amount"]) == Decimal("0")


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
    assert body["status"] == PaymentStatuses.PENDING.value
    assert body["amount"] == "100.50"
    assert body["currency"] == "USD"
    assert "payment_id" in body
    assert "created_at" in body

    stored = await fake_repo.get_by_id(UUID(body["payment_id"]))
    assert stored is not None
    assert stored.provider_id == 1
    assert stored.metadata == {"order_id": "abc-123"}
    assert stored.status is PaymentStatuses.PENDING


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


@pytest.mark.anyio
async def test_create_payment_returns_422_for_unknown_provider_id(client: AsyncClient) -> None:
    payload = _valid_create_payload()
    payload["provider_id"] = 2

    response = await client.post("/payments", json=payload, headers={"X-API-Key": API_KEY})

    assert response.status_code == 422


@pytest.mark.anyio
async def test_create_payment_returns_502_when_provider_unavailable(
    fake_repo: FakePaymentRepository,
    fake_key_repo: FakeIdempotencyKeyRepository,
    fake_refund_repo: FakeRefundRepository,
    fake_refund_key_repo: FakeRefundIdempotencyKeyRepository,
    fake_session: FakeSession,
) -> None:
    failing_provider = RecordingFakeProvider(error=ProviderInitiationError("down"))
    async with make_client(
        fake_repo,
        fake_key_repo,
        fake_refund_repo,
        fake_refund_key_repo,
        fake_session,
        failing_provider,
    ) as client:
        response = await client.post(
            "/payments", json=_valid_create_payload(), headers={"X-API-Key": API_KEY}
        )

    assert response.status_code == 502
    # The payment is kept as a trace of the attempt (Txn1 committed before the provider failure).
    payments = list(fake_repo._payments.values())
    assert len(payments) == 1
    assert payments[0].status is PaymentStatuses.CREATED
