from collections.abc import AsyncGenerator, AsyncIterator

from dishka import Provider, Scope, provide
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from src.contexts.core_payment.infrastructure.database.repositories import (
    SQLAlchemyIdempotencyKeyRepository,
    SQLAlchemyPaymentRepository,
    SQLAlchemyRefundIdempotencyKeyRepository,
    SQLAlchemyRefundRepository,
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
    ) -> AsyncGenerator[AsyncSession, BaseException | None]:
        # Not maker.begin(): its TransactionalContext remembers the specific
        # open transaction and forbids using the session if that transaction
        # was finished manually before the block exits (SQLAlchemy
        # engine/util.py, TransactionalContext._trans_ctx_check) — and Txn1
        # is committed manually from the use case between phases.
        # A plain session has no such guard: after a manual commit() the next
        # operation opens a new transaction via autobegin. Unit of Work stays
        # at the DI root: this provider performs the final commit (Txn2).
        # dishka finalizes the generator via asend(exc) — the request exception
        # arrives as the VALUE of yield rather than being thrown into the
        # generator, so we commit only when exc is None; otherwise exiting
        # maker() rolls back uncommitted work. Verified on real PostgreSQL
        # (tests/integration/core_payment/test_transactions_db.py).
        async with maker() as session:
            exc = yield session
            if exc is None:
                await session.commit()


class RepositoriesProvider(Provider):
    scope = Scope.REQUEST

    @provide
    def get_payment_repository(self, session: AsyncSession) -> SQLAlchemyPaymentRepository:
        return SQLAlchemyPaymentRepository(session)

    @provide
    def get_idempotency_key_repository(
        self, session: AsyncSession
    ) -> SQLAlchemyIdempotencyKeyRepository:
        return SQLAlchemyIdempotencyKeyRepository(session)

    @provide
    def get_refund_repository(self, session: AsyncSession) -> SQLAlchemyRefundRepository:
        return SQLAlchemyRefundRepository(session)

    @provide
    def get_refund_idempotency_key_repository(
        self, session: AsyncSession
    ) -> SQLAlchemyRefundIdempotencyKeyRepository:
        return SQLAlchemyRefundIdempotencyKeyRepository(session)
