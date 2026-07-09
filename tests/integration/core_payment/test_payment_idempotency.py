import anyio
import pytest
from httpx import AsyncClient, Response
from tests.fixtures.client import API_KEY
from tests.fixtures.payment import FakePaymentRepository

KEY = "merchant-key-42"


def _payload(amount: str = "100.50") -> dict[str, object]:
    return {"amount": amount, "currency": "USD", "provider_id": 1}


async def _post(client: AsyncClient, key: str | None, amount: str = "100.50") -> Response:
    headers = {"X-API-Key": API_KEY}
    if key is not None:
        headers["Idempotency-Key"] = key
    return await client.post("/payments", json=_payload(amount), headers=headers)


@pytest.mark.anyio
async def test_repeat_with_same_key_replays_same_payment(
    client: AsyncClient, fake_repo: FakePaymentRepository
) -> None:
    first = await _post(client, KEY)
    second = await _post(client, KEY)

    assert first.status_code == 201
    assert second.status_code == 201
    assert second.json()["payment_id"] == first.json()["payment_id"]
    assert second.headers["Idempotency-Replayed"] == "true"
    assert "Idempotency-Replayed" not in first.headers
    assert len(fake_repo._payments) == 1


@pytest.mark.anyio
async def test_same_key_different_body_returns_422(client: AsyncClient) -> None:
    await _post(client, KEY)

    response = await _post(client, KEY, amount="999.00")

    assert response.status_code == 422


@pytest.mark.anyio
async def test_without_key_creates_two_payments(
    client: AsyncClient, fake_repo: FakePaymentRepository
) -> None:
    first = await _post(client, None)
    second = await _post(client, None)

    assert first.json()["payment_id"] != second.json()["payment_id"]
    assert len(fake_repo._payments) == 2


@pytest.mark.anyio
async def test_too_long_key_returns_422(client: AsyncClient) -> None:
    response = await _post(client, "x" * 256)

    assert response.status_code == 422


@pytest.mark.anyio
async def test_concurrent_requests_with_same_key_create_one_payment(
    client: AsyncClient, fake_repo: FakePaymentRepository
) -> None:
    responses: list[Response] = []

    async def _call() -> None:
        responses.append(await _post(client, KEY))

    async with anyio.create_task_group() as tg:
        tg.start_soon(_call)
        tg.start_soon(_call)

    assert {response.status_code for response in responses} == {201}
    ids = {response.json()["payment_id"] for response in responses}
    assert len(ids) == 1
    assert len(fake_repo._payments) == 1
