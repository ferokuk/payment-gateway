from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from httpx import AsyncClient
from src.contexts.core_payment.domain.payment import Payment
from src.contexts.core_payment.domain.statuses import PaymentStatuses, RefundStatuses
from src.contexts.core_payment.infrastructure.providers.base import ProviderInitiationError
from src.shared.ids import new_uuid
from tests.fixtures.client import API_KEY, CALLBACK_SECRET, make_client
from tests.fixtures.idempotency import FakeIdempotencyKeyRepository
from tests.fixtures.payment import FakePaymentRepository, make_payment
from tests.fixtures.providers import RecordingFakeProvider
from tests.fixtures.refund import (
    FakeRefundIdempotencyKeyRepository,
    FakeRefundRepository,
    make_refund,
)
from tests.fixtures.session import FakeSession


async def _add_success_payment(fake_repo: FakePaymentRepository) -> Payment:
    payment = make_payment(status=PaymentStatuses.SUCCESS)  # amount 100.00
    await fake_repo.add(payment)
    return payment


def _refund_payload(amount: str = "40.00") -> dict[str, object]:
    return {"amount": amount}


# --- POST /payments/{payment_id}/refunds ---


@pytest.mark.anyio
async def test_create_refund_returns_201_and_reserves(
    client: AsyncClient,
    fake_repo: FakePaymentRepository,
    fake_refund_repo: FakeRefundRepository,
) -> None:
    payment = await _add_success_payment(fake_repo)

    response = await client.post(
        f"/payments/{payment.id}/refunds",
        json=_refund_payload(),
        headers={"X-API-Key": API_KEY},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["payment_id"] == str(payment.id)
    assert body["status"] == RefundStatuses.PENDING.value
    assert body["amount"] == "40.00"

    stored = await fake_refund_repo.get_by_id(UUID(body["refund_id"]))
    assert stored is not None
    assert stored.status is RefundStatuses.PENDING
    stored_payment = await fake_repo.get_by_id(payment.id)
    assert stored_payment is not None
    assert stored_payment.refunded_amount == Decimal("40.00")


@pytest.mark.anyio
async def test_create_refund_for_missing_payment_returns_404(client: AsyncClient) -> None:
    response = await client.post(
        f"/payments/{new_uuid()}/refunds",
        json=_refund_payload(),
        headers={"X-API-Key": API_KEY},
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Payment not found"}


@pytest.mark.anyio
async def test_create_refund_for_non_success_payment_returns_409(
    client: AsyncClient, fake_repo: FakePaymentRepository
) -> None:
    payment = make_payment(status=PaymentStatuses.PENDING)
    await fake_repo.add(payment)

    response = await client.post(
        f"/payments/{payment.id}/refunds",
        json=_refund_payload(),
        headers={"X-API-Key": API_KEY},
    )

    assert response.status_code == 409


@pytest.mark.anyio
async def test_create_refund_over_remainder_returns_409(
    client: AsyncClient, fake_repo: FakePaymentRepository
) -> None:
    payment = await _add_success_payment(fake_repo)

    response = await client.post(
        f"/payments/{payment.id}/refunds",
        json=_refund_payload(amount="100.01"),
        headers={"X-API-Key": API_KEY},
    )

    assert response.status_code == 409


@pytest.mark.parametrize("amount", ["0", "-1"])
@pytest.mark.anyio
async def test_create_refund_returns_422_on_invalid_amount(
    client: AsyncClient, fake_repo: FakePaymentRepository, amount: str
) -> None:
    payment = await _add_success_payment(fake_repo)

    response = await client.post(
        f"/payments/{payment.id}/refunds",
        json=_refund_payload(amount=amount),
        headers={"X-API-Key": API_KEY},
    )

    assert response.status_code == 422


@pytest.mark.anyio
async def test_create_refund_returns_401_without_api_key(client: AsyncClient) -> None:
    response = await client.post(f"/payments/{new_uuid()}/refunds", json=_refund_payload())

    assert response.status_code == 401


@pytest.mark.anyio
async def test_create_refund_returns_502_and_holds_reservation_when_provider_down(
    fake_repo: FakePaymentRepository,
    fake_key_repo: FakeIdempotencyKeyRepository,
    fake_refund_repo: FakeRefundRepository,
    fake_refund_key_repo: FakeRefundIdempotencyKeyRepository,
    fake_session: FakeSession,
) -> None:
    failing = RecordingFakeProvider(error=ProviderInitiationError("down"))
    async with make_client(
        fake_repo, fake_key_repo, fake_refund_repo, fake_refund_key_repo, fake_session, failing
    ) as client:
        payment = await _add_success_payment(fake_repo)
        response = await client.post(
            f"/payments/{payment.id}/refunds",
            json=_refund_payload(),
            headers={"X-API-Key": API_KEY},
        )

    assert response.status_code == 502
    refunds = list(fake_refund_repo._refunds.values())
    assert len(refunds) == 1
    assert refunds[0].status is RefundStatuses.CREATED
    stored_payment = await fake_repo.get_by_id(payment.id)
    assert stored_payment is not None
    assert stored_payment.refunded_amount == Decimal("40.00")  # reservation held


# --- GET /refunds/{refund_id} ---


@pytest.mark.anyio
async def test_get_refund_returns_200(
    client: AsyncClient, fake_refund_repo: FakeRefundRepository
) -> None:
    refund = make_refund(status=RefundStatuses.PENDING)
    await fake_refund_repo.add(refund)

    response = await client.get(f"/refunds/{refund.id}", headers={"X-API-Key": API_KEY})

    assert response.status_code == 200
    body = response.json()
    assert body["refund_id"] == str(refund.id)
    assert body["payment_id"] == str(refund.payment_id)
    assert body["status"] == "pending"
    assert body["amount"] == "40.00"


@pytest.mark.anyio
async def test_get_refund_returns_404_when_missing(client: AsyncClient) -> None:
    response = await client.get(f"/refunds/{new_uuid()}", headers={"X-API-Key": API_KEY})

    assert response.status_code == 404
    assert response.json() == {"detail": "Refund not found"}


@pytest.mark.anyio
async def test_get_refund_returns_401_without_api_key(client: AsyncClient) -> None:
    response = await client.get(f"/refunds/{new_uuid()}")

    assert response.status_code == 401


# --- GET /payments/{payment_id}/refunds ---


@pytest.mark.anyio
async def test_list_refunds_returns_only_this_payment_oldest_first(
    client: AsyncClient,
    fake_repo: FakePaymentRepository,
    fake_refund_repo: FakeRefundRepository,
) -> None:
    payment = await _add_success_payment(fake_repo)
    moment = datetime.now(UTC)
    newer = make_refund(payment_id=payment.id, amount=Decimal("10.00"), created_at=moment)
    older = make_refund(
        payment_id=payment.id,
        amount=Decimal("20.00"),
        created_at=moment - timedelta(minutes=1),
    )
    for refund in (newer, older, make_refund()):
        await fake_refund_repo.add(refund)

    response = await client.get(f"/payments/{payment.id}/refunds", headers={"X-API-Key": API_KEY})

    assert response.status_code == 200
    assert response.json() == {
        "refunds": [
            {
                "refund_id": str(older.id),
                "payment_id": str(payment.id),
                "status": "pending",
                "amount": "20.00",
            },
            {
                "refund_id": str(newer.id),
                "payment_id": str(payment.id),
                "status": "pending",
                "amount": "10.00",
            },
        ]
    }


@pytest.mark.anyio
async def test_list_refunds_returns_empty_list_when_none(
    client: AsyncClient, fake_repo: FakePaymentRepository
) -> None:
    payment = await _add_success_payment(fake_repo)

    response = await client.get(f"/payments/{payment.id}/refunds", headers={"X-API-Key": API_KEY})

    assert response.status_code == 200
    assert response.json() == {"refunds": []}


@pytest.mark.anyio
async def test_list_refunds_returns_404_for_missing_payment(client: AsyncClient) -> None:
    response = await client.get(f"/payments/{new_uuid()}/refunds", headers={"X-API-Key": API_KEY})

    assert response.status_code == 404
    assert response.json() == {"detail": "Payment not found"}


@pytest.mark.anyio
async def test_list_refunds_returns_401_without_api_key(client: AsyncClient) -> None:
    response = await client.get(f"/payments/{new_uuid()}/refunds")

    assert response.status_code == 401


# --- POST /callbacks/refunds ---


def _callback_payload(refund_id: str, status: str = "success") -> dict[str, object]:
    return {"refund_id": refund_id, "status": status}


@pytest.mark.anyio
async def test_refund_callback_applies_transition_and_returns_200(
    client: AsyncClient,
    fake_repo: FakePaymentRepository,
    fake_refund_repo: FakeRefundRepository,
) -> None:
    payment = await _add_success_payment(fake_repo)
    await fake_repo.reserve_refund_amount(payment.id, Decimal("40.00"))
    refund = make_refund(status=RefundStatuses.PENDING, payment_id=payment.id)
    await fake_refund_repo.add(refund)

    response = await client.post(
        "/callbacks/refunds",
        json=_callback_payload(str(refund.id)),
        headers={"X-Callback-Secret": CALLBACK_SECRET},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["refund_id"] == str(refund.id)
    assert body["status"] == "success"
    stored = await fake_refund_repo.get_by_id(refund.id)
    assert stored is not None
    assert stored.status is RefundStatuses.SUCCESS


@pytest.mark.anyio
async def test_refund_callback_failed_releases_reservation(
    client: AsyncClient,
    fake_repo: FakePaymentRepository,
    fake_refund_repo: FakeRefundRepository,
) -> None:
    payment = await _add_success_payment(fake_repo)
    await fake_repo.reserve_refund_amount(payment.id, Decimal("40.00"))
    refund = make_refund(status=RefundStatuses.PENDING, payment_id=payment.id)
    await fake_refund_repo.add(refund)

    response = await client.post(
        "/callbacks/refunds",
        json={
            "refund_id": str(refund.id),
            "status": "failed",
            "failure_reason": "card_unavailable",
        },
        headers={"X-Callback-Secret": CALLBACK_SECRET},
    )

    assert response.status_code == 200
    stored_payment = await fake_repo.get_by_id(payment.id)
    assert stored_payment is not None
    assert stored_payment.refunded_amount == Decimal("0")


@pytest.mark.anyio
async def test_refund_callback_duplicate_returns_200_noop(
    client: AsyncClient, fake_refund_repo: FakeRefundRepository
) -> None:
    refund = make_refund(status=RefundStatuses.SUCCESS)
    await fake_refund_repo.add(refund)

    response = await client.post(
        "/callbacks/refunds",
        json=_callback_payload(str(refund.id)),
        headers={"X-Callback-Secret": CALLBACK_SECRET},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "success"


@pytest.mark.anyio
async def test_refund_callback_without_secret_returns_401(client: AsyncClient) -> None:
    response = await client.post("/callbacks/refunds", json=_callback_payload(str(new_uuid())))

    assert response.status_code == 401


@pytest.mark.anyio
async def test_refund_callback_for_missing_refund_returns_404(client: AsyncClient) -> None:
    response = await client.post(
        "/callbacks/refunds",
        json=_callback_payload(str(new_uuid())),
        headers={"X-Callback-Secret": CALLBACK_SECRET},
    )

    assert response.status_code == 404


@pytest.mark.anyio
async def test_refund_callback_invalid_transition_returns_409(
    client: AsyncClient, fake_refund_repo: FakeRefundRepository
) -> None:
    refund = make_refund(status=RefundStatuses.CREATED)
    await fake_refund_repo.add(refund)

    response = await client.post(
        "/callbacks/refunds",
        json=_callback_payload(str(refund.id)),
        headers={"X-Callback-Secret": CALLBACK_SECRET},
    )

    assert response.status_code == 409


@pytest.mark.anyio
async def test_refund_callback_failed_without_reason_returns_422(client: AsyncClient) -> None:
    response = await client.post(
        "/callbacks/refunds",
        json=_callback_payload(str(new_uuid()), status="failed"),
        headers={"X-Callback-Secret": CALLBACK_SECRET},
    )

    assert response.status_code == 422
