from decimal import Decimal

import anyio
import pytest
from httpx import AsyncClient, Response
from tests.fixtures.client import API_KEY
from tests.fixtures.payment import FakePaymentRepository
from tests.fixtures.refund import FakeRefundRepository
from tests.integration.core_payment.test_refund_router import _add_success_payment

KEY = "refund-merchant-key-7"


async def _post(
    client: AsyncClient, payment_id: str, key: str | None, amount: str = "40.00"
) -> Response:
    headers = {"X-API-Key": API_KEY}
    if key is not None:
        headers["Idempotency-Key"] = key
    return await client.post(
        f"/payments/{payment_id}/refunds", json={"amount": amount}, headers=headers
    )


@pytest.mark.anyio
async def test_repeat_with_same_key_replays_same_refund_without_double_reserve(
    client: AsyncClient,
    fake_repo: FakePaymentRepository,
    fake_refund_repo: FakeRefundRepository,
) -> None:
    payment = await _add_success_payment(fake_repo)

    first = await _post(client, str(payment.id), KEY)
    second = await _post(client, str(payment.id), KEY)

    assert first.status_code == 201
    assert second.status_code == 201
    assert second.json()["refund_id"] == first.json()["refund_id"]
    assert second.headers["Idempotency-Replayed"] == "true"
    assert "Idempotency-Replayed" not in first.headers
    assert len(fake_refund_repo._refunds) == 1
    stored = await fake_repo.get_by_id(payment.id)
    assert stored is not None
    assert stored.refunded_amount == Decimal("40.00")


@pytest.mark.anyio
async def test_same_key_different_body_returns_422(
    client: AsyncClient, fake_repo: FakePaymentRepository
) -> None:
    payment = await _add_success_payment(fake_repo)
    await _post(client, str(payment.id), KEY)

    response = await _post(client, str(payment.id), KEY, amount="50.00")

    assert response.status_code == 422


@pytest.mark.anyio
async def test_without_key_creates_two_refunds(
    client: AsyncClient,
    fake_repo: FakePaymentRepository,
    fake_refund_repo: FakeRefundRepository,
) -> None:
    payment = await _add_success_payment(fake_repo)

    first = await _post(client, str(payment.id), None)
    second = await _post(client, str(payment.id), None)

    assert first.json()["refund_id"] != second.json()["refund_id"]
    assert len(fake_refund_repo._refunds) == 2


@pytest.mark.anyio
async def test_concurrent_requests_with_same_key_create_one_refund(
    client: AsyncClient,
    fake_repo: FakePaymentRepository,
    fake_refund_repo: FakeRefundRepository,
) -> None:
    payment = await _add_success_payment(fake_repo)
    responses: list[Response] = []

    async def _call() -> None:
        responses.append(await _post(client, str(payment.id), KEY))

    async with anyio.create_task_group() as tg:
        tg.start_soon(_call)
        tg.start_soon(_call)

    assert {response.status_code for response in responses} == {201}
    assert len({response.json()["refund_id"] for response in responses}) == 1
    assert len(fake_refund_repo._refunds) == 1
    stored = await fake_repo.get_by_id(payment.id)
    assert stored is not None
    assert stored.refunded_amount == Decimal("40.00")
