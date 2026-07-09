from src.contexts.core_payment.domain.payment import Payment
from src.contexts.core_payment.infrastructure.providers.base import PaymentProvider


class RecordingFakeProvider(PaymentProvider):
    """Records initiated payments; optionally raises the given error."""

    def __init__(self, error: Exception | None = None) -> None:
        self.initiated: list[Payment] = []
        self._error = error

    async def initiate_payment(self, payment: Payment) -> None:
        if self._error is not None:
            raise self._error
        self.initiated.append(payment)
