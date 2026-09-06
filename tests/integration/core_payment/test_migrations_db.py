"""Alembic roundtrips on the same dedicated database as the transaction tests."""

import asyncio
import os
from collections.abc import Iterator
from decimal import Decimal

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from src.shared.config import settings
from src.shared.database.engine import create_engine
from src.shared.ids import new_uuid

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(TEST_DATABASE_URL is None, reason="TEST_DATABASE_URL is not set")
PARENT = "8c113a4030ae"


@pytest.fixture
def migration_config(monkeypatch: pytest.MonkeyPatch) -> Iterator[Config]:
    assert TEST_DATABASE_URL is not None
    monkeypatch.setattr(settings, "database_url", TEST_DATABASE_URL)
    config = Config("alembic.ini")
    try:
        yield config
    finally:
        command.downgrade(config, "base")


def test_full_migration_roundtrip(migration_config: Config) -> None:
    command.upgrade(migration_config, "head")
    command.downgrade(migration_config, "base")
    command.upgrade(migration_config, "head")
    command.check(migration_config)


def test_scheduling_migration_preserves_existing_refunds(migration_config: Config) -> None:
    payment_id, refund_id = new_uuid(), new_uuid()

    async def seed() -> None:
        assert TEST_DATABASE_URL is not None
        engine = create_engine(TEST_DATABASE_URL)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO payments "
                        "(id, amount, provider_id, currency, status, refunded_amount) "
                        "VALUES (:id, 100, 1, 'USD', 'SUCCESS', 40)"
                    ),
                    {"id": payment_id},
                )
                await connection.execute(
                    text(
                        "INSERT INTO refunds (id, payment_id, amount, status, created_at) "
                        "VALUES (:id, :payment_id, 40, 'ERROR', now() - interval '2 days')"
                    ),
                    {"id": refund_id, "payment_id": payment_id},
                )
        finally:
            await engine.dispose()

    async def verify(*, upgraded: bool) -> None:
        assert TEST_DATABASE_URL is not None
        engine = create_engine(TEST_DATABASE_URL)
        try:
            async with engine.connect() as connection:
                row = (
                    await connection.execute(
                        text(
                            "SELECT r.status::text, r.amount, p.refunded_amount FROM refunds r "
                            "JOIN payments p ON p.id = r.payment_id WHERE r.id = :id"
                        ),
                        {"id": refund_id},
                    )
                ).one()
                assert tuple(row) == ("ERROR", Decimal("40.00"), Decimal("40.00"))
                if upgraded:
                    schedule = (
                        await connection.execute(
                            text(
                                "SELECT next_reconcile_at, reconciliation_attempts, "
                                "coalesce(next_reconcile_at, created_at) < now() AS due "
                                "FROM refunds WHERE id = :id"
                            ),
                            {"id": refund_id},
                        )
                    ).one()
                    assert tuple(schedule) == (None, 0, True)
                    index = await connection.scalar(
                        text(
                            "SELECT indexdef FROM pg_indexes "
                            "WHERE indexname = 'ix_refunds_reconciliation_due'"
                        )
                    )
                    assert index is not None and "COALESCE" in index.upper()
                    assert all(status in index for status in ("CREATED", "PENDING", "ERROR"))
                else:
                    columns = (
                        (
                            await connection.execute(
                                text(
                                    "SELECT column_name FROM information_schema.columns "
                                    "WHERE table_name = 'refunds'"
                                )
                            )
                        )
                        .scalars()
                        .all()
                    )
                    assert "next_reconcile_at" not in columns
                    assert "reconciliation_attempts" not in columns
        finally:
            await engine.dispose()

    command.upgrade(migration_config, PARENT)
    asyncio.run(seed())
    command.upgrade(migration_config, "head")
    asyncio.run(verify(upgraded=True))
    command.downgrade(migration_config, PARENT)
    asyncio.run(verify(upgraded=False))
    command.upgrade(migration_config, "head")
    asyncio.run(verify(upgraded=True))
