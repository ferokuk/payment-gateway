from dishka import Provider, Scope, provide

from src.contexts.core_payment.application.use_cases.create_payment import CreatePaymentUseCase
from src.contexts.core_payment.application.use_cases.get_payment_status import (
    GetPaymentStatusUseCase,
)
from src.contexts.core_payment.infrastructure.database.repositories import (
    SQLAlchemyPaymentRepository,
)


class CorePaymentProvider(Provider):
    scope = Scope.REQUEST

    @provide
    def get_create_payment_use_case(
        self,
        payment_repository: SQLAlchemyPaymentRepository,
    ) -> CreatePaymentUseCase:
        return CreatePaymentUseCase(payment_repository)

    @provide
    def get_payment_status_use_case(
        self,
        payment_repository: SQLAlchemyPaymentRepository,
    ) -> GetPaymentStatusUseCase:
        return GetPaymentStatusUseCase(payment_repository)
