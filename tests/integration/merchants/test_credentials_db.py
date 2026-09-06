"""Credential lifecycle and real HTTP authentication against a disposable PostgreSQL."""

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from dishka import Provider, Scope, make_async_container, provide
from dishka.integrations.fastapi import DishkaRoute, FastapiProvider, FromDishka, setup_dishka
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from src.contexts.merchants.application.authentication import AuthenticateAPIKey
from src.contexts.merchants.application.public import MerchantIdentity
from src.contexts.merchants.domain.api_key import credential_digest
from src.contexts.merchants.domain.merchant import LEGACY_MERCHANT_ID
from src.contexts.merchants.infrastructure.database.models import MerchantAPIKeyModel, MerchantModel
from src.contexts.merchants.infrastructure.database.repositories import SQLAlchemyMerchantRepository
from src.shared.config import Settings
from src.shared.database.database import Base
from src.shared.database.engine import create_engine, create_sessionmaker
from src.shared.ioc import DatabaseProvider
from tests.fixtures.merchants import LocalMerchantAuthProvider

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(TEST_DATABASE_URL is None, reason="TEST_DATABASE_URL is not set"),
]


@pytest.fixture
async def credential_db() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    assert TEST_DATABASE_URL is not None
    engine = create_engine(TEST_DATABASE_URL)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield create_sessionmaker(engine)
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
        await engine.dispose()


class _Config(Provider):
    scope = Scope.APP

    @provide
    def settings(self) -> Settings:
        assert TEST_DATABASE_URL is not None
        return Settings(database_url=TEST_DATABASE_URL, callback_secret="callback")


@pytest.fixture
async def identity_client(
    credential_db: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncClient]:
    app = FastAPI()
    app.router.route_class = DishkaRoute

    @app.get("/identity")
    async def identity(identity: FromDishka[MerchantIdentity]) -> dict[str, str]:
        return {"merchant_id": str(identity.merchant_id), "api_key_id": str(identity.api_key_id)}

    container = make_async_container(
        _Config(), DatabaseProvider(), LocalMerchantAuthProvider(), FastapiProvider()
    )
    setup_dishka(container, app)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield client
    finally:
        await container.close()


async def test_rotation_revocation_activation_and_uniform_http_failures(
    credential_db: async_sessionmaker[AsyncSession],
    identity_client: AsyncClient,
) -> None:
    async with credential_db() as session:
        repository = SQLAlchemyMerchantRepository(session)
        merchant = await repository.create("Merchant")
        other = await repository.create("Other")
        old = await repository.issue_key(merchant.id, "old")
        new = await repository.issue_key(merchant.id, "new")
        unrelated = await repository.issue_key(other.id, "unrelated")
        await session.commit()

    for token in (old.token, new.token):
        response = await identity_client.get("/identity", headers={"X-API-Key": token})
        assert response.status_code == 200
        assert response.json()["merchant_id"] == str(merchant.id)

    async with credential_db() as session:
        repository = SQLAlchemyMerchantRepository(session)
        await repository.revoke_key(old.key.id)
        await session.commit()
        first_revocation = await session.scalar(
            select(MerchantAPIKeyModel.revoked_at).where(MerchantAPIKeyModel.id == old.key.id)
        )
        await repository.revoke_key(old.key.id)
        await session.commit()
        assert (
            await session.scalar(
                select(MerchantAPIKeyModel.revoked_at).where(MerchantAPIKeyModel.id == old.key.id)
            )
            == first_revocation
        )
        await repository.set_active(merchant.id, active=False)
        await session.commit()

    invalid_headers = [
        {},
        {"X-API-Key": ""},
        {"X-API-Key": "malformed"},
        {"X-API-Key": "test-api-key"},
        {"X-API-Key": old.token},
        {"X-API-Key": new.token},
        {"X-API-Key": old.token, "X-Merchant-ID": str(other.id)},
    ]
    for headers in invalid_headers:
        response = await identity_client.get("/identity", headers=headers)
        assert response.status_code == 401
        assert response.json() == {"detail": "Invalid or missing API key"}
    non_ascii = await identity_client.get("/identity", headers=[(b"X-API-Key", b"\xff")])
    assert non_ascii.status_code == 401
    assert non_ascii.json() == {"detail": "Invalid or missing API key"}
    assert (
        await identity_client.get(
            "/identity",
            headers={"X-API-Key": unrelated.token},
        )
    ).status_code == 200

    async with credential_db() as session:
        await SQLAlchemyMerchantRepository(session).set_active(merchant.id, active=True)
        await session.commit()
    assert (
        await identity_client.get(
            "/identity",
            headers={"X-API-Key": old.token},
        )
    ).status_code == 401
    assert (
        await identity_client.get(
            "/identity",
            headers={"X-API-Key": new.token},
        )
    ).status_code == 200


async def test_expired_credentials_and_database_has_only_digests(
    credential_db: async_sessionmaker[AsyncSession],
    identity_client: AsyncClient,
) -> None:
    async with credential_db() as session:
        repository = SQLAlchemyMerchantRepository(session)
        merchant = await repository.create("Expires")
        issued = await repository.issue_key(merchant.id, "expires")
        await session.execute(
            update(MerchantAPIKeyModel)
            .where(MerchantAPIKeyModel.id == issued.key.id)
            .values(
                created_at=datetime.now(UTC) - timedelta(days=2),
                expires_at=datetime.now(UTC) - timedelta(days=1),
            )
        )
        await session.commit()
        row = (
            (
                await session.execute(
                    select(MerchantAPIKeyModel.__table__).where(
                        MerchantAPIKeyModel.id == issued.key.id
                    )
                )
            )
            .mappings()
            .one()
        )
        assert row["secret_digest"] == credential_digest(issued.token)
        assert issued.token not in str(dict(row))
    response = await identity_client.get("/identity", headers={"X-API-Key": issued.token})
    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid or missing API key"}


async def test_authentication_refreshes_previously_loaded_key_after_external_revocation(
    credential_db: async_sessionmaker[AsyncSession],
) -> None:
    async with credential_db() as session:
        repository = SQLAlchemyMerchantRepository(session)
        merchant = await repository.create("Cached ORM")
        issued = await repository.issue_key(merchant.id, "key")
        await session.commit()
        # Hold an ORM reference so SQLAlchemy's identity map cannot evict this object.
        loaded = await session.get(MerchantAPIKeyModel, issued.key.id)
        assert loaded is not None and loaded.revoked_at is None
        authenticate = AuthenticateAPIKey(repository)
        assert await authenticate(issued.token) is not None
        async with credential_db() as writer:
            await SQLAlchemyMerchantRepository(writer).revoke_key(issued.key.id)
            await writer.commit()
        assert await authenticate(issued.token) is None
        assert loaded.revoked_at is not None


async def test_database_errors_do_not_expose_credential_digest(
    credential_db: async_sessionmaker[AsyncSession],
) -> None:
    digest = credential_digest("credential-used-only-to-test-error-redaction")
    async with credential_db() as session:
        with pytest.raises(DBAPIError) as error:
            # Invalid SQL fails on PostgreSQL with the digest present in bound
            # parameters, just as a failing legacy authentication query would.
            await session.execute(
                text(
                    "SELECT secret_digest FROM missing_credential_table "
                    "WHERE secret_digest = :digest"
                ),
                {"digest": digest},
            )
        assert digest not in str(error.value)
        assert "SQL parameters hidden" in str(error.value)


async def test_legacy_import_is_explicit_single_use_and_revocable(
    credential_db: async_sessionmaker[AsyncSession],
    identity_client: AsyncClient,
) -> None:
    token = "previous-global-key"
    assert (await identity_client.get("/identity", headers={"X-API-Key": token})).status_code == 401
    async with credential_db() as session:
        session.add(MerchantModel(id=LEGACY_MERCHANT_ID, name="Legacy", is_active=True))
        await session.flush()
        repository = SQLAlchemyMerchantRepository(session)
        key = await repository.import_legacy_key(token, "migration")
        await session.commit()
        with pytest.raises(ValueError, match="already imported"):
            await repository.import_legacy_key("another-key", "duplicate")
    response = await identity_client.get("/identity", headers={"X-API-Key": token})
    assert response.status_code == 200
    assert response.json()["merchant_id"] == str(LEGACY_MERCHANT_ID)
    async with credential_db() as session:
        repository = SQLAlchemyMerchantRepository(session)
        await repository.revoke_key(key.id)
        await session.commit()
        with pytest.raises(ValueError, match="already imported"):
            await repository.import_legacy_key(token, "revoked cannot be reimported")
        with pytest.raises(ValueError, match="not found"):
            await repository.revoke_key(uuid4())
    assert (await identity_client.get("/identity", headers={"X-API-Key": token})).status_code == 401
