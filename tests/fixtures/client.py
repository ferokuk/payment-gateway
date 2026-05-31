from collections.abc import AsyncIterator

import pytest
from dishka import Provider, Scope, make_async_container, provide
from dishka.integrations.fastapi import FastapiProvider, setup_dishka
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from src.contexts.core_payment.infrastructure.database.repositories import (
    SQLAlchemyPaymentRepository,
)
from src.contexts.core_payment.ioc import CorePaymentProvider
from src.contexts.core_payment.presentation.routers.payment import router as payment_router
from src.shared.config import Settings
from src.shared.security import AuthProvider
from tests.fixtures.payment import FakePaymentRepository

API_KEY = "test-api-key"


class FakeConfigProvider(Provider):
    scope = Scope.APP

    @provide
    def get_settings(self) -> Settings:
        return Settings(database_url="sqlite+aiosqlite:///:memory:", api_key=API_KEY)


class FakeRepositoriesProvider(Provider):
    scope = Scope.REQUEST

    def __init__(self, repo: FakePaymentRepository) -> None:
        super().__init__()
        self._repo = repo

    @provide
    def get_payment_repository(self) -> SQLAlchemyPaymentRepository:
        return self._repo  # type: ignore[return-value]


@pytest.fixture
async def client(fake_repo: FakePaymentRepository) -> AsyncIterator[AsyncClient]:
    app = FastAPI()
    app.include_router(payment_router)
    container = make_async_container(
        FakeConfigProvider(),
        AuthProvider(),
        FakeRepositoriesProvider(fake_repo),
        CorePaymentProvider(),
        FastapiProvider(),
    )
    setup_dishka(container, app)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    await container.close()
