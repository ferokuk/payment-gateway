from datetime import UTC, datetime

from src.contexts.core_payment.application.dto.payment import (
    CreatePaymentInputDTO,
    CreatePaymentOutputDTO,
)
from src.contexts.core_payment.domain.payment import Payment, PaymentStatuses
from src.contexts.core_payment.infrastructure.database.repositories import (
    SQLAlchemyPaymentRepository,
)
from src.shared.ids import new_uuid


class CreatePaymentUseCase:
    def __init__(self, payment_repository: SQLAlchemyPaymentRepository) -> None:
        self._payment_repository = payment_repository

    async def __call__(self, command: CreatePaymentInputDTO) -> CreatePaymentOutputDTO:
        payment = Payment(
            id=new_uuid(),
            amount=command.amount,
            currency=command.currency,
            status=PaymentStatuses.CREATED,
            provider_id=command.provider_id,
            created_at=datetime.now(UTC),
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
