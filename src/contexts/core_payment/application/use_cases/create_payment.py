from datetime import datetime
from uuid import uuid4

from src.contexts.core_payment.application.dto.payment import CreatePaymentCommand, CreatePaymentResult
from src.contexts.core_payment.application.ports.payment_provider import PaymentRepository
from src.contexts.core_payment.application.ports.unit_of_work import UnitOfWork
from src.contexts.core_payment.domain.payment import Payment, PaymentStatus


class CreatePaymentUseCase:
    def __init__(
            self,
            payment_repository: PaymentRepository,
            unit_of_work: UnitOfWork
    ) -> None:
        self._payment_repository = payment_repository
        self._unit_of_work = unit_of_work

    async def __call__(self,command: CreatePaymentCommand) -> CreatePaymentResult:
        payment = Payment(
            id=uuid4(),
            amount=command.amount,
            currency=command.currency,
            status=PaymentStatus.CREATED,
            provider_id=command.provider_id,
            created_at=datetime.now(),
            metadata=command.metadata,
        )
        await self._payment_repository.add(payment)
        await self._unit_of_work.commit()

        return CreatePaymentResult(
            payment_id=payment.id,
            amount=payment.amount,
            currency=payment.currency,
            status=payment.status,
            created_at=payment.created_at,
        )
