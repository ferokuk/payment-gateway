"""Transaction mechanics against a real PostgreSQL.

Run: docker compose up -d database, then
  $env:TEST_DATABASE_URL = "postgresql+asyncpg://postgres:postgres@localhost:5432/payment_gateway_test"
  uv run pytest tests/integration/core_payment/test_transactions_db.py -v
Tables are created and dropped wholesale — the DB must be dedicated to tests.
"""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

import anyio
import pytest
from dishka import Provider, Scope, make_async_container, provide
from dishka.integrations.fastapi import FastapiProvider, setup_dishka
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import select
from src.contexts.core_payment.domain.exceptions import StalePaymentStateError
from src.contexts.core_payment.domain.statuses import PaymentStatuses
from src.contexts.core_payment.infrastructure.database.models import (
    IdempotencyKeyModel,
    PaymentModel,
)
from src.contexts.core_payment.infrastructure.database.repositories import (
    SQLAlchemyPaymentRepository,
)
from src.contexts.core_payment.infrastructure.providers.base import (
    PaymentProvider,
    ProviderInitiationError,
)
from src.contexts.core_payment.ioc import CorePaymentProvider, SystemCorePaymentProvider
from src.contexts.core_payment.presentation.routers.callbacks import (
    router as callbacks_router,
)
from src.contexts.core_payment.presentation.routers.payment import router as payment_router
from src.shared.config import Settings
from src.shared.database.database import Base
from src.shared.database.engine import create_engine, create_sessionmaker
from src.shared.ioc import DatabaseProvider, RepositoriesProvider, SystemRepositoriesProvider
from src.shared.security import AuthProvider
from tests.fixtures.client import API_KEY, CALLBACK_SECRET, FakePaymentProviderProvider
from tests.fixtures.merchants import MERCHANT_ID, seed_legacy_merchant
from tests.fixtures.providers import RecordingFakeProvider

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    TEST_DATABASE_URL is None,
    reason="TEST_DATABASE_URL is not set — real-DB tests run only locally",
)


class _DbConfigProvider(Provider):
    scope = Scope.APP

    @provide
    def get_settings(self) -> Settings:
        assert TEST_DATABASE_URL is not None
        return Settings(
            database_url=TEST_DATABASE_URL,
            api_key=API_KEY,
            callback_secret=CALLBACK_SECRET,
        )


@pytest.fixture
async def _tables() -> AsyncIterator[None]:
    assert TEST_DATABASE_URL is not None
    engine = create_engine(TEST_DATABASE_URL)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
        await seed_legacy_merchant(conn)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@asynccontextmanager
async def _db_client(payment_provider: PaymentProvider) -> AsyncIterator[AsyncClient]:
    app = FastAPI()
    app.include_router(payment_router)
    app.include_router(callbacks_router)
    container = make_async_container(
        _DbConfigProvider(),
        DatabaseProvider(),
        RepositoriesProvider(),
        SystemRepositoriesProvider(),
        FakePaymentProviderProvider(payment_provider),
        CorePaymentProvider(),
        SystemCorePaymentProvider(),
        AuthProvider(),
        FastapiProvider(),
    )
    setup_dishka(container, app)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    await container.close()


def _payload() -> dict[str, object]:
    return {"amount": "100.50", "currency": "USD", "provider_id": 1}


async def _select_payment_statuses() -> list[str]:
    """Read with an independent engine: only committed data is visible."""
    assert TEST_DATABASE_URL is not None
    engine = create_engine(TEST_DATABASE_URL)
    maker = create_sessionmaker(engine)
    async with maker() as session:
        statuses = list((await session.execute(select(PaymentModel.status))).scalars())
    await engine.dispose()
    return [status.value for status in statuses]


@pytest.mark.anyio
async def test_success_commits_pending_via_autobegin_txn2(_tables: None) -> None:
    async with _db_client(RecordingFakeProvider()) as client:
        response = await client.post("/payments", json=_payload(), headers={"X-API-Key": API_KEY})

    assert response.status_code == 201
    # The session DI provider (get_session) did the final commit: PENDING is visible externally.
    assert await _select_payment_statuses() == ["pending"]


@pytest.mark.anyio
async def test_provider_failure_leaves_committed_created_row(_tables: None) -> None:
    failing = RecordingFakeProvider(error=ProviderInitiationError("down"))
    async with _db_client(failing) as client:
        response = await client.post("/payments", json=_payload(), headers={"X-API-Key": API_KEY})

    assert response.status_code == 502
    # Txn1 was committed before the failure: a trace of the attempt remains in CREATED.
    assert await _select_payment_statuses() == ["created"]


@pytest.mark.anyio
async def test_concurrent_same_key_requests_create_single_row(_tables: None) -> None:
    responses: list[Response] = []
    async with _db_client(RecordingFakeProvider()) as client:

        async def _call() -> None:
            responses.append(
                await client.post(
                    "/payments",
                    json=_payload(),
                    headers={"X-API-Key": API_KEY, "Idempotency-Key": "race-key"},
                )
            )

        async with anyio.create_task_group() as tg:
            tg.start_soon(_call)
            tg.start_soon(_call)

    assert {response.status_code for response in responses} == {201}
    assert len({response.json()["payment_id"] for response in responses}) == 1
    assert len(await _select_payment_statuses()) == 1

    assert TEST_DATABASE_URL is not None
    engine = create_engine(TEST_DATABASE_URL)
    maker = create_sessionmaker(engine)
    async with maker() as session:
        keys = list((await session.execute(select(IdempotencyKeyModel.key))).scalars())
    await engine.dispose()
    assert keys == ["race-key"]


@pytest.mark.anyio
async def test_stale_transition_does_not_regress_committed_status(_tables: None) -> None:
    # Regression scenario: the payment has reached SUCCESS,
    # a lagging competitor with a CREATED snapshot tries to write PENDING.
    async with _db_client(RecordingFakeProvider()) as client:
        response = await client.post("/payments", json=_payload(), headers={"X-API-Key": API_KEY})
        payment_id = response.json()["payment_id"]
        for body in (
            {"payment_id": payment_id, "status": "processing"},
            {"payment_id": payment_id, "status": "success"},
        ):
            callback = await client.post(
                "/callbacks/payments",
                json=body,
                headers={"X-Callback-Secret": CALLBACK_SECRET},
            )
            assert callback.status_code == 200

    assert await _select_payment_statuses() == ["success"]

    assert TEST_DATABASE_URL is not None
    engine = create_engine(TEST_DATABASE_URL)
    maker = create_sessionmaker(engine)
    async with maker() as session:
        repo = SQLAlchemyPaymentRepository(session, MERCHANT_ID)
        stale = await repo.get_by_id(UUID(payment_id))
        assert stale is not None
        # Simulate a stale snapshot: the competitor "remembers" the payment in CREATED.
        stale.status = PaymentStatuses.CREATED
        stale.mark_pending()
        with pytest.raises(StalePaymentStateError):
            await repo.update(stale, expected_status=PaymentStatuses.CREATED)
        await session.rollback()
    await engine.dispose()

    # The terminal status did not regress.
    assert await _select_payment_statuses() == ["success"]
