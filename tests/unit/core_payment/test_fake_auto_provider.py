import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from src.contexts.core_payment.domain.payment import Payment
from src.contexts.core_payment.domain.refund import Refund
from src.contexts.core_payment.domain.statuses import (
    PaymentStatuses,
    RefundFailureReasons,
    RefundStatuses,
)
from src.contexts.core_payment.infrastructure.providers.base import (
    ProviderInitiationError,
    ProviderRejectedError,
    RefundProviderState,
)
from src.contexts.core_payment.infrastructure.providers.fake_auto import (
    _REFUND_SCENARIOS,
    _REFUND_TRUTH,
    AutoCallbackFakePaymentProvider,
)
from tests.fixtures.payment import make_payment
from tests.fixtures.refund import make_refund

CALLBACK_URL = "http://test/callbacks/payments"
REFUND_CALLBACK_URL = "http://test/callbacks/refunds"
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
            refund_callback_url=REFUND_CALLBACK_URL,
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
        # The timeout is only a hang guard (delay_seconds=0, MockTransport):
        # generous on purpose, so a loaded CI runner does not turn it into flake.
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)


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


def _refund_with_scenario(scenario: str | None) -> Refund:
    refund = make_refund(status=RefundStatuses.CREATED)
    if scenario is not None:
        refund.metadata = {"fake_scenario": scenario}
    return refund


async def _initiate_refund_and_wait(
    provider: AutoCallbackFakePaymentProvider, refund: Refund
) -> None:
    await provider.initiate_refund(refund)
    tasks = set(provider._tasks)
    if tasks:
        # Hang guard only (delay_seconds=0, MockTransport): generous on purpose.
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)


@pytest.mark.parametrize(
    ("scenario", "expected"),
    [
        (None, [{"status": "success"}]),
        ("success", [{"status": "success"}]),
        (
            "card_unavailable",
            [{"status": "failed", "failure_reason": "card_unavailable"}],
        ),
        (
            "insufficient_merchant_balance",
            [{"status": "failed", "failure_reason": "insufficient_merchant_balance"}],
        ),
        ("timeout", [{"status": "failed", "failure_reason": "timeout"}]),
        ("error", [{"status": "error", "error_message": "Internal provider error"}]),
    ],
)
@pytest.mark.anyio
async def test_refund_scenario_sends_expected_callback_sequence(
    scenario: str | None, expected: list[dict[str, Any]]
) -> None:
    recorded: list[httpx.Request] = []
    refund = _refund_with_scenario(scenario)

    async with _make_provider(recorded) as provider:
        await _initiate_refund_and_wait(provider, refund)

    bodies = _sent_bodies(recorded)
    assert [
        {key: value for key, value in body.items() if key != "refund_id"} for body in bodies
    ] == expected
    assert all(body["refund_id"] == str(refund.id) for body in bodies)
    assert all(request.headers["X-Callback-Secret"] == SECRET for request in recorded)
    assert all(str(request.url) == REFUND_CALLBACK_URL for request in recorded)


@pytest.mark.parametrize("scenario", ["initiation_error", "unknown_scenario"])
@pytest.mark.anyio
async def test_bad_refund_scenario_raises_initiation_error_and_sends_nothing(
    scenario: str,
) -> None:
    recorded: list[httpx.Request] = []

    async with _make_provider(recorded) as provider:
        with pytest.raises(ProviderInitiationError):
            await provider.initiate_refund(_refund_with_scenario(scenario))

        assert recorded == []
        assert provider._tasks == set()


@pytest.mark.parametrize("scenario", ["initiation_error", "unknown_scenario"])
@pytest.mark.anyio
async def test_refused_refund_initiation_is_a_definitive_rejection(scenario: str) -> None:
    # A refusal is the one failure whose outcome is known: nothing was taken,
    # so reconciliation may close the refund and give the reservation back.
    recorded: list[httpx.Request] = []

    async with _make_provider(recorded) as provider:
        with pytest.raises(ProviderRejectedError):
            await provider.initiate_refund(_refund_with_scenario(scenario))


@pytest.mark.parametrize(
    ("scenario", "expected_state", "expected_reason"),
    [
        ("success", RefundProviderState.SUCCEEDED, None),
        ("card_unavailable", RefundProviderState.FAILED, RefundFailureReasons.CARD_UNAVAILABLE),
        ("timeout", RefundProviderState.FAILED, RefundFailureReasons.TIMEOUT),
        # The callback for this scenario said "internal error, outcome unknown",
        # yet the operation itself settled — the gap reconciliation closes.
        ("error", RefundProviderState.FAILED, RefundFailureReasons.TIMEOUT),
    ],
)
@pytest.mark.anyio
async def test_status_query_reveals_the_settled_outcome(
    scenario: str,
    expected_state: RefundProviderState,
    expected_reason: RefundFailureReasons | None,
) -> None:
    recorded: list[httpx.Request] = []
    refund = _refund_with_scenario(scenario)

    async with _make_provider(recorded) as provider:
        await _initiate_refund_and_wait(provider, refund)
        status = await provider.get_refund_status(refund)

    assert status.state is expected_state
    assert status.failure_reason is expected_reason


def test_every_refund_scenario_has_a_settled_truth() -> None:
    # Initiation looks the truth up by the same key it validated the scenario
    # with, so a scenario without an entry would be a KeyError at runtime.
    assert set(_REFUND_TRUTH) == set(_REFUND_SCENARIOS)


@pytest.mark.anyio
async def test_status_query_reports_unseen_refund_as_absent() -> None:
    recorded: list[httpx.Request] = []

    async with _make_provider(recorded) as provider:
        status = await provider.get_refund_status(_refund_with_scenario("success"))

    assert status.state is RefundProviderState.ABSENT


@pytest.mark.anyio
async def test_status_query_reconstructs_the_outcome_for_another_process() -> None:
    # The reconciler runs in its own process with a blank provider instance;
    # a refund that left created was accepted, so it must not be denied.
    recorded: list[httpx.Request] = []
    accepted = _refund_with_scenario("card_unavailable")
    accepted.status = RefundStatuses.PENDING

    async with _make_provider(recorded) as provider:
        status = await provider.get_refund_status(accepted)

    assert status.state is RefundProviderState.FAILED
    assert status.failure_reason is RefundFailureReasons.CARD_UNAVAILABLE


@pytest.mark.anyio
async def test_repeated_refund_initiation_sends_one_callback_chain() -> None:
    # Reconciliation re-initiates refunds stuck in CREATED; without dedup by
    # refund id the payer would get their money a second time.
    recorded: list[httpx.Request] = []
    refund = _refund_with_scenario("success")

    async with _make_provider(recorded) as provider:
        await _initiate_refund_and_wait(provider, refund)
        await _initiate_refund_and_wait(provider, refund)

    assert len(recorded) == 1
