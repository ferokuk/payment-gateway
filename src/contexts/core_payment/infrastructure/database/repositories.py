from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from src.contexts.core_payment.domain.payment import Payment
from src.contexts.core_payment.infrastructure.database.models import PaymentModel


class SQLAlchemyPaymentRepository:
    def __init__(self, session: AsyncSession):
        self._session = session

    async def add(self, payment: Payment) -> None:
        model = self._to_model(payment)
        self._session.add(model)

    async def get_by_id(self, payment_id: UUID) -> Payment | None:
        model = await self._session.get(PaymentModel, payment_id)
        return self._to_domain(model) if model else None

    @staticmethod
    def _to_model(payment: Payment) -> PaymentModel:
        return PaymentModel(
            id=payment.id,
            provider_id=payment.provider_id,
            amount=payment.amount,
            currency=payment.currency,
            meta=payment.metadata,
            status=payment.status,
            created_at=payment.created_at,
        )

    @staticmethod
    def _to_domain(payment: PaymentModel) -> Payment:
        return Payment(
            id=payment.id,
            provider_id=payment.provider_id,
            amount=payment.amount,
            currency=payment.currency,
            metadata=payment.meta,
            status=payment.status,
            created_at=payment.created_at,
        )
