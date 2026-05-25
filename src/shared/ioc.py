from collections.abc import AsyncIterator

from dishka import Provider, Scope, provide
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from src.contexts.core_payment.application.ports.repositories import PaymentRepository
from src.contexts.core_payment.infrastructure.database.repositories import (
    SQLAlchemyPaymentRepository,
)
from src.shared.config import Settings, settings
from src.shared.database.engine import create_engine, create_sessionmaker


class ConfigProvider(Provider):
    scope = Scope.APP

    @provide
    def get_settings(self) -> Settings:
        return settings


class DatabaseProvider(Provider):
    @provide(scope=Scope.APP)
    async def get_engine(self, cfg: Settings) -> AsyncIterator[AsyncEngine]:
        engine = create_engine(cfg.database_url)
        yield engine
        await engine.dispose()

    @provide(scope=Scope.APP)
    async def get_sessionmaker(self, engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
        return create_sessionmaker(engine)

    @provide(scope=Scope.REQUEST)
    async def get_session(
        self, maker: async_sessionmaker[AsyncSession]
    ) -> AsyncIterator[AsyncSession]:
        async with maker.begin() as session:
            yield session


class RepositoriesProvider(Provider):
    scope = Scope.REQUEST

    @provide
    def get_payment_repository(self, session: AsyncSession) -> PaymentRepository:
        return SQLAlchemyPaymentRepository(session)
