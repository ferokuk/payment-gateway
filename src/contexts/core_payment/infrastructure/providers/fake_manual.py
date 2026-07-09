import structlog

from src.contexts.core_payment.domain.payment import Payment
from src.contexts.core_payment.infrastructure.providers.base import PaymentProvider

logger = structlog.get_logger(__name__)


class ManualFakePaymentProvider(PaymentProvider):
    """Passive fake: "accepts" the payment and does nothing.

    Any further payment movement happens only via manual calls
    to the callback endpoint (Swagger / tests).
    """

    async def initiate_payment(self, payment: Payment) -> None:
        logger.info("fake_payment_initiated", payment_id=str(payment.id), mode="manual")
