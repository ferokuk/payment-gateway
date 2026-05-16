from dishka import Provider, Scope, provide

from src.contexts.core_payment.application.ports.payment_repository import PaymentRepository
from src.contexts.core_payment.application.use_cases.create_payment import CreatePaymentUseCase


class CorePaymentProvider(Provider):
    scope = Scope.REQUEST

    @provide
    def get_create_payment_use_case(
        self,
        payment_repository: PaymentRepository,
    ) -> CreatePaymentUseCase:
        return CreatePaymentUseCase(payment_repository)
