import pytest
from httpx import AsyncClient
from src.contexts.core_payment.domain.statuses import PaymentStatuses
from src.shared.ids import new_uuid
from tests.fixtures.client import CALLBACK_SECRET
from tests.fixtures.payment import FakePaymentRepository, make_payment


def _callback_payload(payment_id: str, status: str = "processing") -> dict[str, object]:
    return {"payment_id": payment_id, "status": status}


@pytest.mark.anyio
async def test_callback_applies_transition_and_returns_200(
    client: AsyncClient, fake_repo: FakePaymentRepository
) -> None:
    payment = make_payment(status=PaymentStatuses.PENDING)
    await fake_repo.add(payment)

    response = await client.post(
        "/callbacks/payments",
        json=_callback_payload(str(payment.id)),
        headers={"X-Callback-Secret": CALLBACK_SECRET},
    )

    assert response.status_code == 200
    assert response.json() == {"payment_id": str(payment.id), "status": "processing"}
    stored = await fake_repo.get_by_id(payment.id)
    assert stored is not None
    assert stored.status is PaymentStatuses.PROCESSING


@pytest.mark.anyio
async def test_duplicate_callback_returns_200_noop(
    client: AsyncClient, fake_repo: FakePaymentRepository
) -> None:
    payment = make_payment(status=PaymentStatuses.PROCESSING)
    await fake_repo.add(payment)

    response = await client.post(
        "/callbacks/payments",
        json=_callback_payload(str(payment.id)),
        headers={"X-Callback-Secret": CALLBACK_SECRET},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "processing"


@pytest.mark.anyio
async def test_callback_without_secret_returns_401(client: AsyncClient) -> None:
    response = await client.post("/callbacks/payments", json=_callback_payload(str(new_uuid())))

    assert response.status_code == 401


@pytest.mark.anyio
async def test_callback_with_wrong_secret_returns_401(client: AsyncClient) -> None:
    response = await client.post(
        "/callbacks/payments",
        json=_callback_payload(str(new_uuid())),
        headers={"X-Callback-Secret": "wrong-secret"},
    )

    assert response.status_code == 401


@pytest.mark.anyio
async def test_callback_for_missing_payment_returns_404(client: AsyncClient) -> None:
    response = await client.post(
        "/callbacks/payments",
        json=_callback_payload(str(new_uuid())),
        headers={"X-Callback-Secret": CALLBACK_SECRET},
    )

    assert response.status_code == 404


@pytest.mark.anyio
async def test_invalid_transition_returns_409(
    client: AsyncClient, fake_repo: FakePaymentRepository
) -> None:
    payment = make_payment(status=PaymentStatuses.PENDING)
    await fake_repo.add(payment)

    response = await client.post(
        "/callbacks/payments",
        json=_callback_payload(str(payment.id), status="success"),
        headers={"X-Callback-Secret": CALLBACK_SECRET},
    )

    assert response.status_code == 409


@pytest.mark.anyio
async def test_invalid_failure_reason_for_state_returns_422(
    client: AsyncClient, fake_repo: FakePaymentRepository
) -> None:
    payment = make_payment(status=PaymentStatuses.PENDING)
    await fake_repo.add(payment)

    response = await client.post(
        "/callbacks/payments",
        json={
            "payment_id": str(payment.id),
            "status": "failed",
            "failure_reason": "fraud",
        },
        headers={"X-Callback-Secret": CALLBACK_SECRET},
    )

    assert response.status_code == 422


@pytest.mark.anyio
async def test_failed_without_reason_returns_422(client: AsyncClient) -> None:
    response = await client.post(
        "/callbacks/payments",
        json=_callback_payload(str(new_uuid()), status="failed"),
        headers={"X-Callback-Secret": CALLBACK_SECRET},
    )

    assert response.status_code == 422
