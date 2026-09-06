from collections.abc import AsyncIterator
from datetime import timedelta

import httpx
from dishka import Provider, Scope, provide
from sqlalchemy.ext.asyncio import AsyncSession

from src.contexts.core_payment.application.use_cases.create_payment import CreatePaymentUseCase
from src.contexts.core_payment.application.use_cases.create_refund import CreateRefundUseCase
from src.contexts.core_payment.application.use_cases.get_payment_status import (
    GetPaymentStatusUseCase,
)
from src.contexts.core_payment.application.use_cases.get_refund_status import (
    GetRefundStatusUseCase,
)
from src.contexts.core_payment.application.use_cases.list_payment_refunds import (
    ListPaymentRefundsUseCase,
)
from src.contexts.core_payment.application.use_cases.process_provider_callback import (
    ProcessProviderCallbackUseCase,
)
from src.contexts.core_payment.application.use_cases.process_refund_callback import (
    ProcessRefundCallbackUseCase,
)
from src.contexts.core_payment.application.use_cases.reconcile_stuck_refunds import (
    ReconcileStuckRefundsUseCase,
)
from src.contexts.core_payment.infrastructure.database.repositories import (
    SQLAlchemyIdempotencyKeyRepository,
    SQLAlchemyPaymentRepository,
    SQLAlchemyRefundIdempotencyKeyRepository,
    SQLAlchemyRefundRepository,
    SystemSQLAlchemyPaymentRepository,
    SystemSQLAlchemyRefundRepository,
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
    async def get_payment_provider(
        self, settings: Settings, http_client: httpx.AsyncClient
    ) -> AsyncIterator[PaymentProvider]:
        if settings.fake_provider_mode == "auto":
            provider = AutoCallbackFakePaymentProvider(
                http_client=http_client,
                callback_url=f"{settings.self_base_url}/callbacks/payments",
                refund_callback_url=f"{settings.self_base_url}/callbacks/refunds",
                callback_secret=settings.callback_secret,
                delay_seconds=settings.fake_callback_delay_seconds,
            )
            try:
                yield provider
            finally:
                # Dishka finalizes this dependency before its HTTP client.
                await provider.aclose()
        else:
            yield ManualFakePaymentProvider()


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
    def get_create_refund_use_case(
        self,
        payment_repository: SQLAlchemyPaymentRepository,
        refund_repository: SQLAlchemyRefundRepository,
        refund_idempotency_repository: SQLAlchemyRefundIdempotencyKeyRepository,
        payment_provider: PaymentProvider,
        session: AsyncSession,
        settings: Settings,
    ) -> CreateRefundUseCase:
        return CreateRefundUseCase(
            payment_repository,
            refund_repository,
            refund_idempotency_repository,
            payment_provider,
            session,
            initiation_max_age=timedelta(seconds=settings.refund_initiation_max_age_seconds),
        )

    @provide
    def get_list_payment_refunds_use_case(
        self,
        payment_repository: SQLAlchemyPaymentRepository,
        refund_repository: SQLAlchemyRefundRepository,
    ) -> ListPaymentRefundsUseCase:
        return ListPaymentRefundsUseCase(payment_repository, refund_repository)

    @provide
    def get_refund_status_use_case(
        self,
        refund_repository: SQLAlchemyRefundRepository,
    ) -> GetRefundStatusUseCase:
        return GetRefundStatusUseCase(refund_repository)


class SystemCorePaymentProvider(Provider):
    """Service-only workflows; never supplies merchant-facing use cases."""

    scope = Scope.REQUEST

    @provide
    def get_process_provider_callback_use_case(
        self,
        payment_repository: SystemSQLAlchemyPaymentRepository,
    ) -> ProcessProviderCallbackUseCase:
        return ProcessProviderCallbackUseCase(payment_repository)

    @provide
    def get_reconcile_stuck_refunds_use_case(
        self,
        refund_repository: SystemSQLAlchemyRefundRepository,
        payment_repository: SystemSQLAlchemyPaymentRepository,
        payment_provider: PaymentProvider,
        session: AsyncSession,
        settings: Settings,
    ) -> ReconcileStuckRefundsUseCase:
        return ReconcileStuckRefundsUseCase(
            refund_repository,
            payment_repository,
            payment_provider,
            session,
            stuck_after=timedelta(seconds=settings.reconcile_stuck_after_seconds),
            initiation_max_age=timedelta(seconds=settings.refund_initiation_max_age_seconds),
            batch_size=settings.reconcile_batch_size,
            retry_after=timedelta(seconds=settings.reconcile_retry_after_seconds),
            retry_max=timedelta(seconds=settings.reconcile_retry_max_seconds),
        )

    @provide
    def get_process_refund_callback_use_case(
        self,
        refund_repository: SystemSQLAlchemyRefundRepository,
        payment_repository: SystemSQLAlchemyPaymentRepository,
    ) -> ProcessRefundCallbackUseCase:
        return ProcessRefundCallbackUseCase(refund_repository, payment_repository)
