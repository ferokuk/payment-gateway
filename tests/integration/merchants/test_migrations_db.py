"""Populated merchant migrations against the dedicated PostgreSQL test database."""

import asyncio
import json
import os
from collections.abc import Iterator
from typing import Any
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection
from src.contexts.merchants.infrastructure.database.repositories import SQLAlchemyMerchantRepository
from src.shared.config import settings
from src.shared.database.engine import create_engine, create_sessionmaker
from src.shared.ids import new_uuid

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(TEST_DATABASE_URL is None, reason="TEST_DATABASE_URL is not set")
PARENT = "f4b2a6c8d901"
REVISION = "b73f1d5e8a20"
LEGACY_MERCHANT_ID = UUID("00000000-0000-4000-8000-000000000001")
OWNED_TABLES = ("payments", "refunds", "idempotency_keys", "refund_idempotency_keys")


@pytest.fixture
def merchant_migration_config(monkeypatch: pytest.MonkeyPatch) -> Iterator[Config]:
    assert TEST_DATABASE_URL is not None
    monkeypatch.setattr(settings, "database_url", TEST_DATABASE_URL)
    monkeypatch.setenv("API_KEY", "migration-must-not-import-environment-credentials")
    config = Config("alembic.ini")
    try:
        yield config
    finally:
        # Some tests intentionally create data that the guarded downgrade must
        # refuse. Explicitly remove only these disposable test DB records first.
        async def remove_test_data() -> None:
            assert TEST_DATABASE_URL is not None
            engine = create_engine(TEST_DATABASE_URL)
            try:
                async with engine.begin() as connection:
                    if await connection.scalar(text("SELECT to_regclass('merchants') IS NOT NULL")):
                        await connection.execute(text("TRUNCATE merchants CASCADE"))
            finally:
                await engine.dispose()

        asyncio.run(remove_test_data())
        command.downgrade(config, "base")


async def _snapshot() -> dict[str, list[dict[str, Any]]]:
    """Compare every pre-existing column, including timestamps and JSON snapshots."""
    assert TEST_DATABASE_URL is not None
    engine = create_engine(TEST_DATABASE_URL)
    try:
        async with engine.connect() as connection:
            result: dict[str, list[dict[str, Any]]] = {}
            for table in OWNED_TABLES:
                order_by = "key" if "idempotency" in table else "id"
                result[table] = list(
                    (
                        await connection.execute(
                            text(
                                f"SELECT to_jsonb(t) - 'merchant_id' FROM {table} AS t "
                                f"ORDER BY {order_by}"
                            )
                        )
                    ).scalars()
                )
            return result
    finally:
        await engine.dispose()


def test_upgrade_preserves_legacy_state_without_importing_credentials(
    merchant_migration_config: Config,
) -> None:
    payment_id, pending_payment_id, refund_id = new_uuid(), new_uuid(), new_uuid()

    async def seed() -> None:
        assert TEST_DATABASE_URL is not None
        engine = create_engine(TEST_DATABASE_URL)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO payments "
                        "(id, amount, provider_id, currency, status, refunded_amount, metadata) "
                        "VALUES (:id, 100, 1, 'USD', 'SUCCESS', 40, '{\"old\": true}'), "
                        "(:pending_id, 50, 2, 'EUR', 'CREATED', 0, NULL)"
                    ),
                    {"id": payment_id, "pending_id": pending_payment_id},
                )
                await connection.execute(
                    text(
                        "INSERT INTO refunds "
                        "(id, payment_id, amount, status, created_at, next_reconcile_at, "
                        "reconciliation_attempts, error_message) VALUES "
                        "(:id, :payment_id, 40, 'ERROR', '2026-08-01T12:00:00Z', "
                        "'2026-08-02T12:00:00Z', 3, 'provider unavailable')"
                    ),
                    {"id": refund_id, "payment_id": payment_id},
                )
                await connection.execute(
                    text(
                        "INSERT INTO idempotency_keys "
                        "(key, request_hash, payment_id, response_body) "
                        "VALUES ('legacy-key', :hash, :id, CAST(:snapshot AS jsonb)), "
                        "('pending-key', :pending_hash, :pending_id, NULL)"
                    ),
                    {
                        "hash": "a" * 64,
                        "id": payment_id,
                        "snapshot": json.dumps(
                            {"payment_id": str(payment_id), "status": "pending", "amount": "100.00"}
                        ),
                        "pending_hash": "b" * 64,
                        "pending_id": pending_payment_id,
                    },
                )
                await connection.execute(
                    text(
                        "INSERT INTO refund_idempotency_keys "
                        "(key, request_hash, refund_id, response_body) "
                        "VALUES ('legacy-key', :hash, :id, CAST(:snapshot AS jsonb))"
                    ),
                    {
                        "hash": "c" * 64,
                        "id": refund_id,
                        "snapshot": json.dumps(
                            {"refund_id": str(refund_id), "status": "pending", "amount": "40.00"}
                        ),
                    },
                )
        finally:
            await engine.dispose()

    async def verify_ownership() -> None:
        assert TEST_DATABASE_URL is not None
        engine = create_engine(TEST_DATABASE_URL)
        try:
            async with engine.connect() as connection:
                merchant = (
                    await connection.execute(text("SELECT id, is_active FROM merchants"))
                ).one()
                assert tuple(merchant) == (LEGACY_MERCHANT_ID, True)
                assert await connection.scalar(text("SELECT count(*) FROM merchant_api_keys")) == 0
                for table in OWNED_TABLES:
                    owners = (
                        (
                            await connection.execute(
                                text(f"SELECT DISTINCT merchant_id FROM {table}")
                            )
                        )
                        .scalars()
                        .all()
                    )
                    assert owners == [LEGACY_MERCHANT_ID]
                columns = (
                    await connection.execute(
                        text(
                            "SELECT table_name, is_nullable, column_default "
                            "FROM information_schema.columns WHERE table_schema = current_schema() "
                            "AND column_name = 'merchant_id' AND table_name <> 'merchant_api_keys'"
                        )
                    )
                ).all()
                assert len(columns) == 4
                assert all(
                    row.is_nullable == "NO" and row.column_default is None for row in columns
                )
        finally:
            await engine.dispose()

    command.upgrade(merchant_migration_config, PARENT)
    asyncio.run(seed())
    before = asyncio.run(_snapshot())
    command.upgrade(merchant_migration_config, REVISION)
    asyncio.run(verify_ownership())
    assert asyncio.run(_snapshot()) == before
    command.downgrade(merchant_migration_config, PARENT)
    assert asyncio.run(_snapshot()) == before
    command.upgrade(merchant_migration_config, REVISION)
    asyncio.run(verify_ownership())
    assert asyncio.run(_snapshot()) == before
    command.check(merchant_migration_config)


async def _insert_financial_rows(
    connection: AsyncConnection, merchant_id: UUID, payment_id: UUID, refund_id: UUID
) -> None:
    await connection.execute(
        text(
            "INSERT INTO payments "
            "(id, merchant_id, amount, provider_id, currency, status, refunded_amount) "
            "VALUES (:id, :merchant_id, 100, 1, 'USD', 'SUCCESS', 40)"
        ),
        {"id": payment_id, "merchant_id": merchant_id},
    )
    await connection.execute(
        text(
            "INSERT INTO refunds (id, merchant_id, payment_id, amount, status) "
            "VALUES (:id, :merchant_id, :payment_id, 40, 'PENDING')"
        ),
        {"id": refund_id, "merchant_id": merchant_id, "payment_id": payment_id},
    )
    for table, operation_column, operation_id in (
        ("idempotency_keys", "payment_id", payment_id),
        ("refund_idempotency_keys", "refund_id", refund_id),
    ):
        await connection.execute(
            text(
                f"INSERT INTO {table} (merchant_id, key, request_hash, {operation_column}) "
                "VALUES (:merchant_id, 'shared-key', :hash, :id)"
            ),
            {"merchant_id": merchant_id, "hash": "d" * 64, "id": operation_id},
        )


def test_downgrade_preserves_financial_data_with_active_imported_legacy_key(
    merchant_migration_config: Config,
) -> None:
    async def seed() -> None:
        assert TEST_DATABASE_URL is not None
        engine = create_engine(TEST_DATABASE_URL)
        try:
            async with engine.begin() as connection:
                await _insert_financial_rows(connection, LEGACY_MERCHANT_ID, new_uuid(), new_uuid())
            async with create_sessionmaker(engine)() as session:
                repository = SQLAlchemyMerchantRepository(session)
                await repository.import_legacy_key("original-global-key", "Before downgrade")
                await session.commit()
            async with engine.connect() as connection:
                key = (
                    await connection.execute(
                        text(
                            "SELECT k.is_legacy, k.revoked_at, k.expires_at, m.is_active "
                            "FROM merchant_api_keys k JOIN merchants m ON m.id = k.merchant_id"
                        )
                    )
                ).one()
                assert tuple(key) == (True, None, None, True)
        finally:
            await engine.dispose()

    async def verify_old_schema() -> None:
        assert TEST_DATABASE_URL is not None
        engine = create_engine(TEST_DATABASE_URL)
        try:
            async with engine.connect() as connection:
                assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                    PARENT
                )
                assert await connection.scalar(text("SELECT to_regclass('merchants')")) is None
                assert (
                    await connection.scalar(text("SELECT to_regclass('merchant_api_keys')")) is None
                )
                assert (
                    await connection.scalar(
                        text(
                            "SELECT count(*) FROM information_schema.columns "
                            "WHERE table_schema = current_schema() AND column_name = 'merchant_id'"
                        )
                    )
                    == 0
                )
        finally:
            await engine.dispose()

    command.upgrade(merchant_migration_config, REVISION)
    asyncio.run(seed())
    before = asyncio.run(_snapshot())
    command.downgrade(merchant_migration_config, PARENT)
    asyncio.run(verify_old_schema())
    assert asyncio.run(_snapshot()) == before


def test_migrated_constraints_reject_cross_merchant_links_and_missing_owners(
    merchant_migration_config: Config,
) -> None:
    async def verify() -> None:
        assert TEST_DATABASE_URL is not None
        engine = create_engine(TEST_DATABASE_URL)
        other_merchant, payment_id, refund_id = new_uuid(), new_uuid(), new_uuid()
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text("INSERT INTO merchants (id, name) VALUES (:id, 'Other merchant')"),
                    {"id": other_merchant},
                )
                await _insert_financial_rows(connection, LEGACY_MERCHANT_ID, payment_id, refund_id)
                # Both merchants may use the same key in both idempotency namespaces.
                await _insert_financial_rows(connection, other_merchant, new_uuid(), new_uuid())
                rejected_statements = (
                    "INSERT INTO refunds (id, merchant_id, payment_id, amount, status) "
                    "VALUES (:new_id, :other, :payment_id, 1, 'CREATED')",
                    "INSERT INTO idempotency_keys (merchant_id, key, request_hash, payment_id) "
                    "VALUES (:other, 'foreign-key', :hash, :payment_id)",
                    "INSERT INTO refund_idempotency_keys "
                    "(merchant_id, key, request_hash, refund_id) "
                    "VALUES (:other, 'foreign-key', :hash, :refund_id)",
                    "UPDATE refunds SET merchant_id = :other WHERE id = :refund_id",
                    "UPDATE idempotency_keys SET merchant_id = :other, key = 'moved-key' "
                    "WHERE payment_id = :payment_id",
                    "UPDATE refund_idempotency_keys SET merchant_id = :other, key = 'moved-key' "
                    "WHERE refund_id = :refund_id",
                    "INSERT INTO payments (id, amount, provider_id, currency, status) "
                    "VALUES (:new_id, 1, 1, 'USD', 'CREATED')",
                    "INSERT INTO idempotency_keys (merchant_id, key, request_hash, payment_id) "
                    "VALUES (:legacy, 'shared-key', :hash, :payment_id)",
                    "INSERT INTO refund_idempotency_keys "
                    "(merchant_id, key, request_hash, refund_id) "
                    "VALUES (:legacy, 'shared-key', :hash, :refund_id)",
                    "DELETE FROM merchants WHERE id = :legacy",
                    "UPDATE payments SET refunded_amount = 101 WHERE id = :payment_id",
                    "UPDATE payments SET refunded_amount = -1 WHERE id = :payment_id",
                )
                for statement in rejected_statements:
                    with pytest.raises(IntegrityError):
                        async with connection.begin_nested():
                            await connection.execute(
                                text(statement),
                                {
                                    "new_id": new_uuid(),
                                    "other": other_merchant,
                                    "legacy": LEGACY_MERCHANT_ID,
                                    "payment_id": payment_id,
                                    "refund_id": refund_id,
                                    "hash": "e" * 64,
                                },
                            )
                for table in ("idempotency_keys", "refund_idempotency_keys"):
                    assert await connection.scalar(text(f"SELECT count(*) FROM {table}")) == 2
        finally:
            await engine.dispose()

    command.upgrade(merchant_migration_config, REVISION)
    asyncio.run(verify())


@pytest.mark.parametrize(
    "data_kind",
    ["merchant", "financial_data", "api_key", "inactive", "revoked_legacy", "expiring_legacy"],
)
def test_downgrade_refuses_to_discard_tenants_or_new_credentials(
    merchant_migration_config: Config, data_kind: str
) -> None:
    async def seed() -> None:
        assert TEST_DATABASE_URL is not None
        engine = create_engine(TEST_DATABASE_URL)
        try:
            async with engine.begin() as connection:
                if data_kind == "inactive":
                    await connection.execute(text("UPDATE merchants SET is_active = false"))
                elif data_kind in ("api_key", "revoked_legacy", "expiring_legacy"):
                    # New-format keys cannot survive the old global-key model,
                    # even when issued to the one legacy merchant.
                    await connection.execute(
                        text(
                            "INSERT INTO merchant_api_keys "
                            "(id, merchant_id, secret_digest, label, is_legacy, "
                            "revoked_at, expires_at) "
                            "VALUES (:id, :merchant_id, :digest, 'Test key', :is_legacy, "
                            "CASE WHEN :revoked THEN now() ELSE NULL END, "
                            "CASE WHEN :expiring THEN now() + interval '1 day' ELSE NULL END)"
                        ),
                        {
                            "id": new_uuid(),
                            "merchant_id": LEGACY_MERCHANT_ID,
                            "digest": "f" * 64,
                            "is_legacy": data_kind != "api_key",
                            "revoked": data_kind == "revoked_legacy",
                            "expiring": data_kind == "expiring_legacy",
                        },
                    )
                else:
                    merchant_id = new_uuid()
                    await connection.execute(
                        text("INSERT INTO merchants (id, name) VALUES (:id, 'New merchant')"),
                        {"id": merchant_id},
                    )
                    if data_kind == "financial_data":
                        await _insert_financial_rows(
                            connection, merchant_id, new_uuid(), new_uuid()
                        )
        finally:
            await engine.dispose()

    async def verify_guard() -> None:
        assert TEST_DATABASE_URL is not None
        engine = create_engine(TEST_DATABASE_URL)
        try:
            async with engine.connect() as connection:
                assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                    REVISION
                )
                if data_kind == "inactive":
                    assert await connection.scalar(text("SELECT is_active FROM merchants")) is False
                elif data_kind in ("api_key", "revoked_legacy", "expiring_legacy"):
                    assert (
                        await connection.scalar(text("SELECT count(*) FROM merchant_api_keys")) == 1
                    )
                else:
                    assert await connection.scalar(text("SELECT count(*) FROM merchants")) == 2
        finally:
            await engine.dispose()

    command.upgrade(merchant_migration_config, REVISION)
    asyncio.run(seed())
    before = asyncio.run(_snapshot())
    with pytest.raises(DBAPIError, match="Cannot downgrade merchant isolation"):
        command.downgrade(merchant_migration_config, PARENT)
    asyncio.run(verify_guard())
    assert asyncio.run(_snapshot()) == before
