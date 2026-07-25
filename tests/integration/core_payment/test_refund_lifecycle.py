from decimal import Decimal

import pytest
from httpx import AsyncClient
from tests.fixtures.client import API_KEY, CALLBACK_SECRET


async def _create_success_payment(client: AsyncClient, amount: str = "100.50") -> str:
    response = await client.post(
        "/payments",
        json={"amount": amount, "currency": "USD", "provider_id": 1},
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


async def _create_refund(client: AsyncClient, payment_id: str, amount: str) -> str:
    response = await client.post(
        f"/payments/{payment_id}/refunds",
        json={"amount": amount},
        headers={"X-API-Key": API_KEY},
    )
    assert response.status_code == 201
    assert response.json()["status"] == "pending"
    return str(response.json()["refund_id"])


async def _send_refund_callback(client: AsyncClient, body: dict[str, object]) -> None:
    response = await client.post(
        "/callbacks/refunds", json=body, headers={"X-Callback-Secret": CALLBACK_SECRET}
    )
    assert response.status_code == 200


async def _refund_status(client: AsyncClient, refund_id: str) -> str:
    response = await client.get(f"/refunds/{refund_id}", headers={"X-API-Key": API_KEY})
    assert response.status_code == 200
    return str(response.json()["status"])


async def _payment_refunded_amount(client: AsyncClient, payment_id: str) -> Decimal:
    response = await client.get(f"/payments/{payment_id}", headers={"X-API-Key": API_KEY})
    assert response.status_code == 200
    return Decimal(str(response.json()["refunded_amount"]))


@pytest.mark.anyio
async def test_full_refund_cycle_success(client: AsyncClient) -> None:
    payment_id = await _create_success_payment(client)
    refund_id = await _create_refund(client, payment_id, "40.00")

    await _send_refund_callback(client, {"refund_id": refund_id, "status": "success"})

    assert await _refund_status(client, refund_id) == "success"
    assert await _payment_refunded_amount(client, payment_id) == Decimal("40.00")


@pytest.mark.parametrize(
    "failure_reason", ["timeout", "card_unavailable", "insufficient_merchant_balance"]
)
@pytest.mark.anyio
async def test_failed_refund_releases_reservation(client: AsyncClient, failure_reason: str) -> None:
    payment_id = await _create_success_payment(client)
    refund_id = await _create_refund(client, payment_id, "40.00")

    await _send_refund_callback(
        client,
        {"refund_id": refund_id, "status": "failed", "failure_reason": failure_reason},
    )

    assert await _refund_status(client, refund_id) == "failed"
    assert await _payment_refunded_amount(client, payment_id) == Decimal("0")


@pytest.mark.anyio
async def test_error_refund_holds_reservation(client: AsyncClient) -> None:
    payment_id = await _create_success_payment(client)
    refund_id = await _create_refund(client, payment_id, "40.00")

    await _send_refund_callback(
        client, {"refund_id": refund_id, "status": "error", "error_message": "boom"}
    )

    assert await _refund_status(client, refund_id) == "error"
    assert await _payment_refunded_amount(client, payment_id) == Decimal("40.00")


@pytest.mark.anyio
async def test_partial_refunds_until_exhaustion_then_409(client: AsyncClient) -> None:
    payment_id = await _create_success_payment(client, amount="100.50")
    first = await _create_refund(client, payment_id, "60.00")
    second = await _create_refund(client, payment_id, "40.50")
    assert first != second
    assert await _payment_refunded_amount(client, payment_id) == Decimal("100.50")

    response = await client.post(
        f"/payments/{payment_id}/refunds",
        json={"amount": "1.00"},
        headers={"X-API-Key": API_KEY},
    )

    assert response.status_code == 409


@pytest.mark.anyio
async def test_failed_refund_frees_remainder_for_a_new_refund(client: AsyncClient) -> None:
    # The same amount becomes valid again after a failed refund — the reason
    # the over-refund error is a 409 (state conflict), not a 422.
    payment_id = await _create_success_payment(client, amount="100.50")
    refund_id = await _create_refund(client, payment_id, "100.50")
    await _send_refund_callback(
        client, {"refund_id": refund_id, "status": "failed", "failure_reason": "timeout"}
    )

    retry_id = await _create_refund(client, payment_id, "100.50")

    assert retry_id != refund_id
    assert await _payment_refunded_amount(client, payment_id) == Decimal("100.50")
