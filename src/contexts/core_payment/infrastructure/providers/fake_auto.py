import asyncio
from typing import Any
from uuid import UUID

import httpx
import structlog

from src.contexts.core_payment.domain.payment import Payment
from src.contexts.core_payment.domain.refund import Refund
from src.contexts.core_payment.domain.statuses import FailureReasons, RefundFailureReasons
from src.contexts.core_payment.infrastructure.providers.base import (
    PaymentProvider,
    ProviderRejectedError,
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

# Refund scenario -> sequence of callback bodies (refund_id is added on send).
# A scenario missing here (incl. "initiation_error") fails the initiation.
_REFUND_SCENARIOS: dict[str, list[dict[str, Any]]] = {
    "success": [{"status": "success"}],
    "card_unavailable": [
        {"status": "failed", "failure_reason": RefundFailureReasons.CARD_UNAVAILABLE.value}
    ],
    "insufficient_merchant_balance": [
        {
            "status": "failed",
            "failure_reason": RefundFailureReasons.INSUFFICIENT_MERCHANT_BALANCE.value,
        }
    ],
    "timeout": [{"status": "failed", "failure_reason": RefundFailureReasons.TIMEOUT.value}],
    "error": [{"status": "error", "error_message": "Internal provider error"}],
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
        refund_callback_url: str,
        callback_secret: str,
        delay_seconds: float,
    ) -> None:
        self._http_client = http_client
        self._callback_url = callback_url
        self._refund_callback_url = refund_callback_url
        self._callback_secret = callback_secret
        self._delay_seconds = delay_seconds
        # Keep references to the tasks so GC does not collect them before completion
        # (https://docs.python.org/3/library/asyncio-task.html#asyncio.create_task).
        self._tasks: set[asyncio.Task[None]] = set()
        # Stand-in for the provider's own idempotency store. In-memory, so it
        # only covers repeats within one process — enough to demonstrate the
        # contract; a real PSP keeps the key for about a day on its side.
        self._initiated_refunds: set[UUID] = set()

    async def initiate_payment(self, payment: Payment) -> None:
        raw_scenario = (payment.metadata or {}).get(_SCENARIO_KEY, _DEFAULT_SCENARIO)
        callbacks = _SCENARIOS.get(raw_scenario) if isinstance(raw_scenario, str) else None
        if callbacks is None:
            raise ProviderRejectedError(
                f"Fake provider rejected initiation: scenario={raw_scenario!r}"
            )
        logger.info(
            "fake_payment_initiated",
            payment_id=str(payment.id),
            mode="auto",
            scenario=raw_scenario,
        )
        payloads = [{"payment_id": str(payment.id), **body} for body in callbacks]
        task = asyncio.create_task(
            self._send_callbacks(self._callback_url, payloads, str(payment.id))
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def initiate_refund(self, refund: Refund) -> None:
        raw_scenario = (refund.metadata or {}).get(_SCENARIO_KEY, _DEFAULT_SCENARIO)
        callbacks = _REFUND_SCENARIOS.get(raw_scenario) if isinstance(raw_scenario, str) else None
        if callbacks is None:
            raise ProviderRejectedError(
                f"Fake provider rejected refund initiation: scenario={raw_scenario!r}"
            )
        if refund.id in self._initiated_refunds:
            # Deduplication by refund id: reconciliation re-initiates refunds
            # stuck in CREATED, and a second callback chain would look like a
            # second refund.
            logger.info("fake_refund_initiation_deduplicated", refund_id=str(refund.id))
            return
        self._initiated_refunds.add(refund.id)
        logger.info(
            "fake_refund_initiated",
            refund_id=str(refund.id),
            mode="auto",
            scenario=raw_scenario,
        )
        payloads = [{"refund_id": str(refund.id), **body} for body in callbacks]
        task = asyncio.create_task(
            self._send_callbacks(self._refund_callback_url, payloads, str(refund.id))
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _send_callbacks(
        self, url: str, payloads: list[dict[str, Any]], entity_id: str
    ) -> None:
        for payload in payloads:
            await asyncio.sleep(self._delay_seconds)
            try:
                response = await self._http_client.post(
                    url,
                    json=payload,
                    headers={"X-Callback-Secret": self._callback_secret},
                )
                logger.info(
                    "fake_callback_sent",
                    entity_id=entity_id,
                    status=payload["status"],
                    response_code=response.status_code,
                )
            except Exception:
                # Fire-and-forget: not just httpx network errors but also, e.g.,
                # a RuntimeError from a client closed on shutdown must not
                # leave "Task exception was never retrieved" behind.
                logger.exception(
                    "fake_callback_delivery_failed",
                    entity_id=entity_id,
                    status=payload["status"],
                )
