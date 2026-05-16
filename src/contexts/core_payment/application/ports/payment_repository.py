from typing import Protocol

from src.contexts.core_payment.domain.payment import Payment


class PaymentRepository(Protocol):
    async def add(self, payment: Payment) -> None:
        pass
