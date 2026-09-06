from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import cast

import pytest
from dishka import Provider, Scope, make_async_container, provide
from dishka.integrations.fastapi import FastapiProvider, setup_dishka
from fastapi import FastAPI, HTTPException, Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from src.contexts.core_payment.infrastructure.database.repositories import (
    SQLAlchemyIdempotencyKeyRepository,
    SQLAlchemyPaymentRepository,
    SQLAlchemyRefundIdempotencyKeyRepository,
    SQLAlchemyRefundRepository,
    SystemSQLAlchemyPaymentRepository,
    SystemSQLAlchemyRefundRepository,
)
from src.contexts.core_payment.infrastructure.providers.base import PaymentProvider
from src.contexts.core_payment.infrastructure.providers.fake_manual import (
    ManualFakePaymentProvider,
)
from src.contexts.core_payment.ioc import CorePaymentProvider, SystemCorePaymentProvider
from src.contexts.core_payment.presentation.routers.callbacks import (
    router as callbacks_router,
)
from src.contexts.core_payment.presentation.routers.payment import router as payment_router
from src.contexts.core_payment.presentation.routers.refund import router as refund_router
from src.contexts.merchants.application.public import MerchantIdentity
from src.shared.config import Settings
from src.shared.security import AuthProvider, CallbackAuthenticated
from tests.fixtures.idempotency import FakeIdempotencyKeyRepository
from tests.fixtures.merchants import MERCHANT_ID
from tests.fixtures.payment import FakePaymentRepository
from tests.fixtures.refund import FakeRefundIdempotencyKeyRepository, FakeRefundRepository
from tests.fixtures.session import FakeSession

API_KEY = "test-api-key"
CALLBACK_SECRET = "test-callback-secret"


class FakeAuthProvider(Provider):
    """Single-merchant auth double; real credential isolation is tested on PostgreSQL."""

    scope = Scope.REQUEST

    @provide
    def authenticate(self, request: Request) -> MerchantIdentity:
        if request.headers.get("X-API-Key") != API_KEY:
            raise HTTPException(status_code=401, detail="Invalid or missing API key")
        return MerchantIdentity(merchant_id=MERCHANT_ID, api_key_id=MERCHANT_ID)

    @provide
    def authenticate_callback(self, request: Request, settings: Settings) -> CallbackAuthenticated:
        return cast(
            "CallbackAuthenticated", AuthProvider().authenticate_callback(request, settings)
        )


class FakeConfigProvider(Provider):
    scope = Scope.APP

    @provide
    def get_settings(self) -> Settings:
        return Settings(
            database_url="sqlite+aiosqlite:///:memory:",
            api_key=API_KEY,
            callback_secret=CALLBACK_SECRET,
        )


class FakeRepositoriesProvider(Provider):
    scope = Scope.REQUEST

    def __init__(
        self,
        repo: FakePaymentRepository,
        key_repo: FakeIdempotencyKeyRepository,
        refund_repo: FakeRefundRepository,
        refund_key_repo: FakeRefundIdempotencyKeyRepository,
    ) -> None:
        super().__init__()
        self._repo = repo
        self._key_repo = key_repo
        self._refund_repo = refund_repo
        self._refund_key_repo = refund_key_repo

    @provide
    def get_payment_repository(self) -> SQLAlchemyPaymentRepository:
        return self._repo  # type: ignore[return-value]

    @provide
    def get_idempotency_key_repository(self) -> SQLAlchemyIdempotencyKeyRepository:
        return self._key_repo  # type: ignore[return-value]

    @provide
    def get_refund_repository(self) -> SQLAlchemyRefundRepository:
        return self._refund_repo  # type: ignore[return-value]

    @provide
    def get_refund_idempotency_key_repository(self) -> SQLAlchemyRefundIdempotencyKeyRepository:
        return self._refund_key_repo  # type: ignore[return-value]

    @provide
    def get_system_payment_repository(self) -> SystemSQLAlchemyPaymentRepository:
        return self._repo  # type: ignore[return-value]

    @provide
    def get_system_refund_repository(self) -> SystemSQLAlchemyRefundRepository:
        return self._refund_repo  # type: ignore[return-value]


class FakeSessionProvider(Provider):
    scope = Scope.REQUEST

    def __init__(self, session: FakeSession) -> None:
        super().__init__()
        self._session = session

    @provide
    def get_session(self) -> AsyncSession:
        return self._session  # type: ignore[return-value]


class FakePaymentProviderProvider(Provider):
    scope = Scope.APP

    def __init__(self, payment_provider: PaymentProvider) -> None:
        super().__init__()
        self._payment_provider = payment_provider

    @provide
    def get_payment_provider(self) -> PaymentProvider:
        return self._payment_provider


@asynccontextmanager
async def make_client(
    fake_repo: FakePaymentRepository,
    fake_key_repo: FakeIdempotencyKeyRepository,
    fake_refund_repo: FakeRefundRepository,
    fake_refund_key_repo: FakeRefundIdempotencyKeyRepository,
    fake_session: FakeSession,
    payment_provider: PaymentProvider,
) -> AsyncIterator[AsyncClient]:
    app = FastAPI()
    app.include_router(payment_router)
    app.include_router(callbacks_router)
    app.include_router(refund_router)
    container = make_async_container(
        FakeConfigProvider(),
        FakeAuthProvider(),
        FakeRepositoriesProvider(fake_repo, fake_key_repo, fake_refund_repo, fake_refund_key_repo),
        FakeSessionProvider(fake_session),
        FakePaymentProviderProvider(payment_provider),
        CorePaymentProvider(),
        SystemCorePaymentProvider(),
        FastapiProvider(),
    )
    setup_dishka(container, app)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    await container.close()


@pytest.fixture
async def client(
    fake_repo: FakePaymentRepository,
    fake_key_repo: FakeIdempotencyKeyRepository,
    fake_refund_repo: FakeRefundRepository,
    fake_refund_key_repo: FakeRefundIdempotencyKeyRepository,
    fake_session: FakeSession,
) -> AsyncIterator[AsyncClient]:
    async with make_client(
        fake_repo,
        fake_key_repo,
        fake_refund_repo,
        fake_refund_key_repo,
        fake_session,
        ManualFakePaymentProvider(),
    ) as ac:
        yield ac
