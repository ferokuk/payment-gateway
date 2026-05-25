from datetime import datetime
from uuid import uuid4

from src.contexts.core_payment.application.dto.payment import (
    CreatePaymentInputDTO,
    CreatePaymentOutputDTO,
)
from src.contexts.core_payment.application.ports.repositories import PaymentRepository
from src.contexts.core_payment.domain.payment import Payment, PaymentStatuses


class CreatePaymentUseCase:
    def __init__(self, payment_repository: PaymentRepository) -> None:
        self._payment_repository = payment_repository

    async def __call__(self, command: CreatePaymentInputDTO) -> CreatePaymentOutputDTO:
        payment = Payment(
            id=uuid4(),
            amount=command.amount,
            currency=command.currency,
            status=PaymentStatuses.CREATED,
            provider_id=command.provider_id,
            created_at=datetime.now(),
            metadata=command.metadata,
        )
        await self._payment_repository.add(payment)

        return CreatePaymentOutputDTO(
            payment_id=payment.id,
            amount=payment.amount,
            currency=payment.currency,
            status=payment.status,
            created_at=payment.created_at,
        )
