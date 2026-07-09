import asyncio
from typing import Any

import httpx
import structlog

from src.contexts.core_payment.domain.payment import Payment
from src.contexts.core_payment.domain.statuses import FailureReasons
from src.contexts.core_payment.infrastructure.providers.base import (
    PaymentProvider,
    ProviderInitiationError,
)

logger = structlog.get_logger(__name__)

_SCENARIO_KEY = "fake_scenario"
_DEFAULT_SCENARIO = "success"

# Scenario -> sequence of callback bodies (payment_id is added on send).
# A scenario missing here (incl. "initiation_error") fails the initiation.
_SCENARIOS: dict[str, list[dict[str, Any]]] = {
    "success": [{"status": "processing"}, {"status": "success"}],
    "insufficient_funds": [
        {"status": "processing"},
        {"status": "failed", "failure_reason": FailureReasons.INSUFFICIENT_FUNDS.value},
    ],
    "fraud": [
        {"status": "processing"},
        {"status": "failed", "failure_reason": FailureReasons.FRAUD.value},
    ],
    "limit_exceeded": [
        {"status": "processing"},
        {"status": "failed", "failure_reason": FailureReasons.LIMIT_EXCEEDED.value},
    ],
    "timeout": [{"status": "failed", "failure_reason": FailureReasons.TIMEOUT.value}],
    "error": [
        {"status": "processing"},
        {"status": "error", "error_message": "Internal provider error"},
    ],
}


class AutoCallbackFakePaymentProvider(PaymentProvider):
    """Active fake: after initiation it sends real HTTP callbacks itself.

    Fire-and-forget: delivery errors are logged and do not affect the payment —
    just like a real PSP whose webhooks can get lost.
    """

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        callback_url: str,
        callback_secret: str,
        delay_seconds: float,
    ) -> None:
        self._http_client = http_client
        self._callback_url = callback_url
        self._callback_secret = callback_secret
        self._delay_seconds = delay_seconds
        # Keep references to the tasks so GC does not collect them before completion
        # (https://docs.python.org/3/library/asyncio-task.html#asyncio.create_task).
        self._tasks: set[asyncio.Task[None]] = set()

    async def initiate_payment(self, payment: Payment) -> None:
        raw_scenario = (payment.metadata or {}).get(_SCENARIO_KEY, _DEFAULT_SCENARIO)
        callbacks = _SCENARIOS.get(raw_scenario) if isinstance(raw_scenario, str) else None
        if callbacks is None:
            raise ProviderInitiationError(
                f"Fake provider rejected initiation: scenario={raw_scenario!r}"
            )
        logger.info(
            "fake_payment_initiated",
            payment_id=str(payment.id),
            mode="auto",
            scenario=raw_scenario,
        )
        task = asyncio.create_task(self._send_callbacks(payment, callbacks))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _send_callbacks(self, payment: Payment, callbacks: list[dict[str, Any]]) -> None:
        for body in callbacks:
            await asyncio.sleep(self._delay_seconds)
            payload = {"payment_id": str(payment.id), **body}
            try:
                response = await self._http_client.post(
                    self._callback_url,
                    json=payload,
                    headers={"X-Callback-Secret": self._callback_secret},
                )
                logger.info(
                    "fake_callback_sent",
                    payment_id=str(payment.id),
                    status=body["status"],
                    response_code=response.status_code,
                )
            except Exception:
                # Fire-and-forget: not just httpx network errors but also, e.g.,
                # a RuntimeError from a client closed on shutdown must not
                # leave "Task exception was never retrieved" behind.
                logger.exception(
                    "fake_callback_delivery_failed",
                    payment_id=str(payment.id),
                    status=body["status"],
                )
