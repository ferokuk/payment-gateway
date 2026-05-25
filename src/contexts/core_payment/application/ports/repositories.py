from typing import Protocol
from uuid import UUID

from src.contexts.core_payment.domain.payment import Payment


class PaymentRepository(Protocol):
    async def add(self, payment: Payment) -> None:
        pass

    async def get_by_id(self, payment_id: UUID) -> Payment | None:
        pass
