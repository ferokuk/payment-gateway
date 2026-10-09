"""Profile migrations retain financial history and guard account/security data."""

import asyncio
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from src.shared.database.engine import create_engine
from src.shared.ids import new_uuid
from tests.integration.merchants.test_migrations_db import (
    LEGACY_MERCHANT_ID,
    OWNED_TABLES,
    TEST_DATABASE_URL,
    _insert_financial_rows,
)
from tests.integration.merchants.test_migrations_db import (
    merchant_migration_config as merchant_migration_config,
)

pytestmark = pytest.mark.skipif(TEST_DATABASE_URL is None, reason="TEST_DATABASE_URL is not set")
PARENT = "b73f1d5e8a20"
REVISION = "c91e4a7b6d20"
PROFILE_TABLES = ("merchant_accounts", "merchant_provider_credentials", "merchant_sessions")


async def _seed_legacy_data() -> None:
    assert TEST_DATABASE_URL is not None
    engine = create_engine(TEST_DATABASE_URL)
    try:
        async with engine.begin() as connection:
            await _insert_financial_rows(connection, LEGACY_MERCHANT_ID, new_uuid(), new_uuid())
            await connection.execute(
                text(
                    "INSERT INTO merchant_api_keys "
                    "(id, merchant_id, secret_digest, label, is_legacy) "
                    "VALUES (:id, :merchant_id, :digest, 'Existing credential', true)"
                ),
                {"id": new_uuid(), "merchant_id": LEGACY_MERCHANT_ID, "digest": "a" * 64},
            )
    finally:
        await engine.dispose()


async def _snapshot(*, include_profiles: bool = False) -> dict[str, list[dict[str, Any]]]:
    assert TEST_DATABASE_URL is not None
    engine = create_engine(TEST_DATABASE_URL)
    tables: tuple[str, ...] = (*OWNED_TABLES, "merchants", "merchant_api_keys")
    if include_profiles:
        tables += PROFILE_TABLES
    try:
        async with engine.connect() as connection:
            result: dict[str, list[dict[str, Any]]] = {}
            for table in tables:
                # Retain every existing column, including merchant ownership.
                expression = "to_jsonb(t)"
                if table == "merchants" and not include_profiles:
                    expression += " - 'deleted_at'"
                rows = await connection.scalars(
                    text(f"SELECT {expression} FROM {table} AS t ORDER BY to_jsonb(t)::text")
                )
                result[table] = list(rows)
            return result
    finally:
        await engine.dispose()


def test_profiles_upgrade_and_empty_downgrade_preserve_legacy_financial_data(
    merchant_migration_config: Config,
) -> None:
    async def verify_empty_profiles() -> None:
        assert TEST_DATABASE_URL is not None
        engine = create_engine(TEST_DATABASE_URL)
        try:
            async with engine.connect() as connection:
                for table in PROFILE_TABLES:
                    assert await connection.scalar(text(f"SELECT count(*) FROM {table}")) == 0
                assert await connection.scalar(text("SELECT deleted_at FROM merchants")) is None
        finally:
            await engine.dispose()

    async def verify_historical_schema() -> None:
        assert TEST_DATABASE_URL is not None
        engine = create_engine(TEST_DATABASE_URL)
        try:
            async with engine.connect() as connection:
                for table in PROFILE_TABLES:
                    assert (
                        await connection.scalar(
                            text("SELECT to_regclass(:table)"), {"table": table}
                        )
                        is None
                    )
                assert (
                    await connection.scalar(
                        text(
                            "SELECT count(*) FROM information_schema.columns "
                            "WHERE table_schema = current_schema() AND table_name = 'merchants' "
                            "AND column_name = 'deleted_at'"
                        )
                    )
                    == 0
                )
        finally:
            await engine.dispose()

    command.upgrade(merchant_migration_config, PARENT)
    asyncio.run(_seed_legacy_data())
    before = asyncio.run(_snapshot())
    command.upgrade(merchant_migration_config, "head")
    asyncio.run(verify_empty_profiles())
    assert asyncio.run(_snapshot()) == before
    command.check(merchant_migration_config)

    command.downgrade(merchant_migration_config, PARENT)
    asyncio.run(verify_historical_schema())
    assert asyncio.run(_snapshot()) == before
    command.upgrade(merchant_migration_config, "head")
    assert asyncio.run(_snapshot()) == before
    command.check(merchant_migration_config)


@pytest.mark.parametrize("data_kind", ["account", "provider_credential", "session", "deleted"])
def test_profiles_downgrade_refuses_to_discard_account_or_security_data(
    merchant_migration_config: Config, data_kind: str
) -> None:
    async def seed() -> None:
        assert TEST_DATABASE_URL is not None
        engine = create_engine(TEST_DATABASE_URL)
        try:
            async with engine.begin() as connection:
                if data_kind == "account":
                    await connection.execute(
                        text(
                            "INSERT INTO merchant_accounts "
                            "(merchant_id, email, password_hash, api_key_id, encrypted_api_key, "
                            "provider_name) VALUES (:merchant_id, 'merchant@example.com', "
                            "'stored-password-hash', (SELECT id FROM merchant_api_keys LIMIT 1), "
                            "'encrypted-api-key', 'bank')"
                        ),
                        {"merchant_id": LEGACY_MERCHANT_ID},
                    )
                elif data_kind == "provider_credential":
                    await connection.execute(
                        text(
                            "INSERT INTO merchant_provider_credentials "
                            "(id, merchant_id, provider_name, encrypted_secret) "
                            "VALUES (:id, :merchant_id, 'bank', 'encrypted-provider-secret')"
                        ),
                        {"id": new_uuid(), "merchant_id": LEGACY_MERCHANT_ID},
                    )
                elif data_kind == "session":
                    await connection.execute(
                        text(
                            "INSERT INTO merchant_sessions "
                            "(token_digest, merchant_id, expires_at) "
                            "VALUES (:digest, :merchant_id, now() + interval '1 hour')"
                        ),
                        {"digest": "b" * 64, "merchant_id": LEGACY_MERCHANT_ID},
                    )
                else:
                    await connection.execute(text("UPDATE merchants SET deleted_at = now()"))
        finally:
            await engine.dispose()

    async def verify_revision() -> None:
        assert TEST_DATABASE_URL is not None
        engine = create_engine(TEST_DATABASE_URL)
        try:
            async with engine.connect() as connection:
                assert (
                    await connection.scalar(text("SELECT version_num FROM alembic_version"))
                    == REVISION
                )
        finally:
            await engine.dispose()

    command.upgrade(merchant_migration_config, "head")
    asyncio.run(_seed_legacy_data())
    asyncio.run(seed())
    before = asyncio.run(_snapshot(include_profiles=True))
    with pytest.raises(DBAPIError, match="Cannot downgrade merchant profiles"):
        command.downgrade(merchant_migration_config, PARENT)
    asyncio.run(verify_revision())
    assert asyncio.run(_snapshot(include_profiles=True)) == before
