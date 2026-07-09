from collections.abc import AsyncIterator

import httpx
from dishka import Provider, Scope, provide
from sqlalchemy.ext.asyncio import AsyncSession

from src.contexts.core_payment.application.use_cases.create_payment import CreatePaymentUseCase
from src.contexts.core_payment.application.use_cases.get_payment_status import (
    GetPaymentStatusUseCase,
)
from src.contexts.core_payment.application.use_cases.process_provider_callback import (
    ProcessProviderCallbackUseCase,
)
from src.contexts.core_payment.infrastructure.database.repositories import (
    SQLAlchemyIdempotencyKeyRepository,
    SQLAlchemyPaymentRepository,
)
from src.contexts.core_payment.infrastructure.providers.base import PaymentProvider
from src.contexts.core_payment.infrastructure.providers.fake_auto import (
    AutoCallbackFakePaymentProvider,
)
from src.contexts.core_payment.infrastructure.providers.fake_manual import (
    ManualFakePaymentProvider,
)
from src.shared.config import Settings


class PaymentProviderProvider(Provider):
    """Selects the PaymentProvider implementation. A separate class so tests
    can swap the provider entirely without touching CorePaymentProvider."""

    scope = Scope.APP

    @provide
    async def get_http_client(self) -> AsyncIterator[httpx.AsyncClient]:
        async with httpx.AsyncClient() as client:
            yield client

    @provide
    def get_payment_provider(
        self, settings: Settings, http_client: httpx.AsyncClient
    ) -> PaymentProvider:
        if settings.fake_provider_mode == "auto":
            return AutoCallbackFakePaymentProvider(
                http_client=http_client,
                callback_url=f"{settings.self_base_url}/callbacks/payments",
                callback_secret=settings.callback_secret,
                delay_seconds=settings.fake_callback_delay_seconds,
            )
        return ManualFakePaymentProvider()


class CorePaymentProvider(Provider):
    scope = Scope.REQUEST

    @provide
    def get_create_payment_use_case(
        self,
        payment_repository: SQLAlchemyPaymentRepository,
        idempotency_repository: SQLAlchemyIdempotencyKeyRepository,
        payment_provider: PaymentProvider,
        session: AsyncSession,
    ) -> CreatePaymentUseCase:
        return CreatePaymentUseCase(
            payment_repository, idempotency_repository, payment_provider, session
        )

    @provide
    def get_payment_status_use_case(
        self,
        payment_repository: SQLAlchemyPaymentRepository,
    ) -> GetPaymentStatusUseCase:
        return GetPaymentStatusUseCase(payment_repository)

    @provide
    def get_process_provider_callback_use_case(
        self,
        payment_repository: SQLAlchemyPaymentRepository,
    ) -> ProcessProviderCallbackUseCase:
        return ProcessProviderCallbackUseCase(payment_repository)
