from uuid import UUID

import structlog

from src.contexts.core_payment.domain.payment import Payment
from src.contexts.core_payment.domain.refund import Refund
from src.contexts.core_payment.infrastructure.providers.base import (
    PaymentProvider,
    RefundProviderState,
    RefundProviderStatus,
)

logger = structlog.get_logger(__name__)


class ManualFakePaymentProvider(PaymentProvider):
    """Passive fake: "accepts" the payment and does nothing.

    Any further payment movement happens only via manual calls
    to the callback endpoint (Swagger / tests).
    """

    def __init__(self) -> None:
        self._initiated_refunds: set[UUID] = set()

    async def initiate_payment(self, payment: Payment) -> None:
        logger.info("fake_payment_initiated", payment_id=str(payment.id), mode="manual")

    async def initiate_refund(self, refund: Refund) -> None:
        # Deduplication by refund id is satisfied trivially: initiation has no
        # side effects here, so a repeat cannot produce a second refund. The id
        # is remembered only to answer status queries.
        self._initiated_refunds.add(refund.id)
        logger.info("fake_refund_initiated", refund_id=str(refund.id), mode="manual")

    async def get_refund_status(self, refund: Refund) -> RefundProviderStatus:
        # This fake never decides an outcome — whoever posts the callback does.
        # So it answers only what it honestly knows: whether it was asked to
        # start this refund at all.
        if refund.id in self._initiated_refunds:
            return RefundProviderStatus(RefundProviderState.PENDING)
        return RefundProviderStatus(RefundProviderState.ABSENT)
