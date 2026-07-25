from src.contexts.core_payment.domain.payment import Payment
from src.contexts.core_payment.domain.refund import Refund
from src.contexts.core_payment.infrastructure.providers.base import PaymentProvider


class RecordingFakeProvider(PaymentProvider):
    """Records initiated payments and refunds; optionally raises the given error.

    refund_error breaks refunds only — needed to bring a payment to success and
    then fail the refund initiation on top of it. Every call is recorded, so a
    test can assert how many times a refund was re-initiated.
    """

    def __init__(
        self, error: Exception | None = None, refund_error: Exception | None = None
    ) -> None:
        self.initiated: list[Payment] = []
        self.initiated_refunds: list[Refund] = []
        self._error = error
        self._refund_error = refund_error

    async def initiate_payment(self, payment: Payment) -> None:
        if self._error is not None:
            raise self._error
        self.initiated.append(payment)

    async def initiate_refund(self, refund: Refund) -> None:
        error = self._refund_error if self._refund_error is not None else self._error
        if error is not None:
            raise error
        self.initiated_refunds.append(refund)
