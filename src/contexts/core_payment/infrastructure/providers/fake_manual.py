import structlog

from src.contexts.core_payment.domain.payment import Payment
from src.contexts.core_payment.domain.refund import Refund
from src.contexts.core_payment.infrastructure.providers.base import PaymentProvider

logger = structlog.get_logger(__name__)


class ManualFakePaymentProvider(PaymentProvider):
    """Passive fake: "accepts" the payment and does nothing.

    Any further payment movement happens only via manual calls
    to the callback endpoint (Swagger / tests).
    """

    async def initiate_payment(self, payment: Payment) -> None:
        logger.info("fake_payment_initiated", payment_id=str(payment.id), mode="manual")

    async def initiate_refund(self, refund: Refund) -> None:
        # Deduplication by refund id is satisfied trivially: initiation has no
        # side effects here, so a repeat cannot produce a second refund.
        logger.info("fake_refund_initiated", refund_id=str(refund.id), mode="manual")
