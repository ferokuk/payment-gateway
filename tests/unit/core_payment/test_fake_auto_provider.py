import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from src.contexts.core_payment.domain.payment import Payment
from src.contexts.core_payment.domain.statuses import PaymentStatuses
from src.contexts.core_payment.infrastructure.providers.base import ProviderInitiationError
from src.contexts.core_payment.infrastructure.providers.fake_auto import (
    AutoCallbackFakePaymentProvider,
)
from tests.fixtures.payment import make_payment

CALLBACK_URL = "http://test/callbacks/payments"
SECRET = "unit-test-secret"


@asynccontextmanager
async def _make_provider(
    recorded: list[httpx.Request], fail_delivery: bool = False
) -> AsyncIterator[AutoCallbackFakePaymentProvider]:
    def handler(request: httpx.Request) -> httpx.Response:
        recorded.append(request)
        if fail_delivery:
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(200, json={"ok": True})

    # Close the client on exit: callbacks must be awaited inside the block.
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        yield AutoCallbackFakePaymentProvider(
            http_client=client,
            callback_url=CALLBACK_URL,
            callback_secret=SECRET,
            delay_seconds=0,
        )
    finally:
        await client.aclose()


def _payment_with_scenario(scenario: str | None) -> Payment:
    payment = make_payment(status=PaymentStatuses.CREATED)
    if scenario is not None:
        payment.metadata = {"fake_scenario": scenario}
    return payment


async def _initiate_and_wait(provider: AutoCallbackFakePaymentProvider, payment: Payment) -> None:
    await provider.initiate_payment(payment)
    tasks = set(provider._tasks)
    if tasks:
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=1)


def _sent_bodies(recorded: list[httpx.Request]) -> list[dict[str, Any]]:
    return [json.loads(request.content) for request in recorded]


@pytest.mark.parametrize(
    ("scenario", "expected"),
    [
        (None, [{"status": "processing"}, {"status": "success"}]),
        ("success", [{"status": "processing"}, {"status": "success"}]),
        (
            "insufficient_funds",
            [
                {"status": "processing"},
                {"status": "failed", "failure_reason": "insufficient_funds"},
            ],
        ),
        (
            "fraud",
            [{"status": "processing"}, {"status": "failed", "failure_reason": "fraud"}],
        ),
        (
            "limit_exceeded",
            [
                {"status": "processing"},
                {"status": "failed", "failure_reason": "limit_exceeded"},
            ],
        ),
        ("timeout", [{"status": "failed", "failure_reason": "timeout"}]),
        (
            "error",
            [
                {"status": "processing"},
                {"status": "error", "error_message": "Internal provider error"},
            ],
        ),
    ],
)
@pytest.mark.anyio
async def test_scenario_sends_expected_callback_sequence(
    scenario: str | None, expected: list[dict[str, Any]]
) -> None:
    recorded: list[httpx.Request] = []
    payment = _payment_with_scenario(scenario)

    async with _make_provider(recorded) as provider:
        # Wait for the callbacks inside the block — before the client is closed.
        await _initiate_and_wait(provider, payment)

    bodies = _sent_bodies(recorded)
    assert [
        {key: value for key, value in body.items() if key != "payment_id"} for body in bodies
    ] == expected
    assert all(body["payment_id"] == str(payment.id) for body in bodies)
    assert all(request.headers["X-Callback-Secret"] == SECRET for request in recorded)
    assert all(str(request.url) == CALLBACK_URL for request in recorded)


@pytest.mark.parametrize("scenario", ["initiation_error", "unknown_scenario"])
@pytest.mark.anyio
async def test_bad_scenario_raises_initiation_error_and_sends_nothing(
    scenario: str,
) -> None:
    recorded: list[httpx.Request] = []

    async with _make_provider(recorded) as provider:
        with pytest.raises(ProviderInitiationError):
            await provider.initiate_payment(_payment_with_scenario(scenario))

        assert recorded == []
        assert provider._tasks == set()


@pytest.mark.anyio
async def test_delivery_failure_is_swallowed() -> None:
    recorded: list[httpx.Request] = []

    async with _make_provider(recorded, fail_delivery=True) as provider:
        # A delivery error is logged and does not crash the background task.
        await _initiate_and_wait(provider, _payment_with_scenario("success"))

    assert len(recorded) == 2
