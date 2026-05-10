from dishka import Scope, provide, Provider
from sqlalchemy.ext.asyncio import AsyncSession

from src.contexts.core_payment.application.ports.payment_provider import PaymentRepository
from src.contexts.core_payment.application.ports.unit_of_work import UnitOfWork
from src.contexts.core_payment.application.use_cases.create_payment import CreatePaymentUseCase
from src.contexts.core_payment.infrastructure.database.unit_of_work import SqlAlchemyUnitOfWork
from src.contexts.core_payment.infrastructure.database.repositories import SQLAlchemyPaymentRepository


class CorePaymentProvider(Provider):
    scope = Scope.REQUEST

    @provide
    def get_payment_repository(self, session: AsyncSession) -> PaymentRepository:
        return SQLAlchemyPaymentRepository(session)

    @provide
    def get_unit_of_work(self, session: AsyncSession) -> UnitOfWork:
        return SqlAlchemyUnitOfWork(session)

    @provide
    def get_create_payment_use_case(
            self,
            payment_repository: PaymentRepository,
            unit_of_work: UnitOfWork
    ) -> CreatePaymentUseCase:
        return CreatePaymentUseCase(payment_repository, unit_of_work)
