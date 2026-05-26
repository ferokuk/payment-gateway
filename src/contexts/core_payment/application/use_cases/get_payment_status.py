from uuid import UUID

from src.contexts.core_payment.application.dto.payment import GetPaymentStatusOutputDTO
from src.contexts.core_payment.application.ports.repositories import PaymentRepository
from src.contexts.core_payment.domain.exceptions import PaymentNotFoundError


class GetPaymentStatusUseCase:
    def __init__(self, payment_repository: PaymentRepository):
        self._payment_repository = payment_repository

    async def __call__(self, payment_id: UUID) -> GetPaymentStatusOutputDTO | None:
        payment = await self._payment_repository.get_by_id(payment_id)
        if not payment:
            raise PaymentNotFoundError

        return GetPaymentStatusOutputDTO(
            payment_id=payment.id,
            status=payment.status,
        )
