import pytest
from httpx import AsyncClient
from tests.fixtures.client import API_KEY, CALLBACK_SECRET


def _create_payload() -> dict[str, object]:
    return {"amount": "100.50", "currency": "USD", "provider_id": 1}


async def _create_payment(client: AsyncClient) -> str:
    response = await client.post(
        "/payments", json=_create_payload(), headers={"X-API-Key": API_KEY}
    )
    assert response.status_code == 201
    assert response.json()["status"] == "pending"
    return str(response.json()["payment_id"])


async def _send_callback(client: AsyncClient, body: dict[str, object]) -> None:
    response = await client.post(
        "/callbacks/payments", json=body, headers={"X-Callback-Secret": CALLBACK_SECRET}
    )
    assert response.status_code == 200


async def _get_status(client: AsyncClient, payment_id: str) -> str:
    response = await client.get(f"/payments/{payment_id}", headers={"X-API-Key": API_KEY})
    assert response.status_code == 200
    return str(response.json()["status"])


@pytest.mark.anyio
async def test_full_cycle_success(client: AsyncClient) -> None:
    payment_id = await _create_payment(client)

    await _send_callback(client, {"payment_id": payment_id, "status": "processing"})
    await _send_callback(client, {"payment_id": payment_id, "status": "success"})

    assert await _get_status(client, payment_id) == "success"


@pytest.mark.parametrize("failure_reason", ["insufficient_funds", "fraud", "limit_exceeded"])
@pytest.mark.anyio
async def test_full_cycle_failed_from_processing(client: AsyncClient, failure_reason: str) -> None:
    payment_id = await _create_payment(client)

    await _send_callback(client, {"payment_id": payment_id, "status": "processing"})
    await _send_callback(
        client,
        {"payment_id": payment_id, "status": "failed", "failure_reason": failure_reason},
    )

    assert await _get_status(client, payment_id) == "failed"


@pytest.mark.anyio
async def test_full_cycle_timeout_from_pending(client: AsyncClient) -> None:
    payment_id = await _create_payment(client)

    await _send_callback(
        client,
        {"payment_id": payment_id, "status": "failed", "failure_reason": "timeout"},
    )

    assert await _get_status(client, payment_id) == "failed"


@pytest.mark.anyio
async def test_full_cycle_error(client: AsyncClient) -> None:
    payment_id = await _create_payment(client)

    await _send_callback(client, {"payment_id": payment_id, "status": "processing"})
    await _send_callback(
        client,
        {"payment_id": payment_id, "status": "error", "error_message": "boom"},
    )

    assert await _get_status(client, payment_id) == "error"
