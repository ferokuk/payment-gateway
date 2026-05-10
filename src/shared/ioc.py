from typing import AsyncIterator

from dishka import Provider, Scope, provide
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from src.shared.config import settings, Settings
from src.shared.database.engine import create_engine, create_sessionmaker


class ConfigProvider(Provider):
    scope = Scope.APP

    @provide
    def get_settings(self) -> Settings:
        return settings


class DatabaseProvider(Provider):
    @provide(scope=Scope.APP)
    async def get_engine(
            self, cfg: Settings
    ) -> AsyncIterator[AsyncEngine]:
        engine = create_engine(cfg.database_url)
        yield engine
        await engine.dispose()

    @provide(scope=Scope.APP)
    async def get_sessionmaker(
            self, engine: AsyncEngine
    ) -> async_sessionmaker[AsyncSession]:
        return create_sessionmaker(engine)

    @provide(scope=Scope.REQUEST)
    async def get_session(
            self, maker: async_sessionmaker[AsyncSession]
    ) -> AsyncIterator[AsyncSession]:
        async with maker.begin() as session:
            yield session
