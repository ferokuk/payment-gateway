"""Merchant accounts and privileged service boundaries against PostgreSQL."""

import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

import anyio
import pytest
from cryptography.fernet import Fernet
from fastapi import Depends
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from src.contexts.core_payment.application.dto.callback import ProviderCallbackInputDTO
from src.contexts.core_payment.application.use_cases.process_provider_callback import (
    ProcessProviderCallbackUseCase,
)
from src.contexts.core_payment.domain.statuses import PaymentStatuses
from src.contexts.core_payment.infrastructure.database.models import PaymentModel
from src.contexts.core_payment.infrastructure.database.repositories import (
    SystemSQLAlchemyPaymentRepository,
)
from src.contexts.merchants.application.service import MerchantService
from src.contexts.merchants.configuration import MerchantSettings
from src.contexts.merchants.domain.api_key import credential_digest
from src.contexts.merchants.infrastructure.database.models import (
    MerchantAccountModel,
    MerchantAPIKeyModel,
    MerchantModel,
    MerchantProviderCredentialModel,
    MerchantSessionModel,
)
from src.contexts.merchants.infrastructure.database.repositories import SQLAlchemyMerchantRepository
from src.contexts.merchants.infrastructure.secrets import SecretVault
from src.contexts.merchants.main import create_app
from src.contexts.merchants.presentation.router import get_service, get_session
from src.shared.database.database import Base
from src.shared.database.engine import create_engine, create_sessionmaker
from src.shared.ids import new_uuid

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(TEST_DATABASE_URL is None, reason="TEST_DATABASE_URL is not set"),
]

PASSWORD = " exact merchant password "
PROVIDER_SECRET = " exact provider secret "
SERVICE_SECRET = "service-credential-for-tests-only-32-characters"
SUPPORT_SECRET = "support-credential-for-tests-only-32-characters"
SERVICE_HEADERS = {"X-Service-Secret": SERVICE_SECRET}
SUPPORT_HEADERS = {"X-Support-Secret": SUPPORT_SECRET}


@dataclass
class Clock:
    instant: datetime


@dataclass
class AccountAPI:
    client: AsyncClient
    maker: async_sessionmaker[AsyncSession]
    vault: SecretVault
    clock: Clock


@pytest.fixture
async def account_api() -> AsyncIterator[AccountAPI]:
    assert TEST_DATABASE_URL is not None
    encryption_key = Fernet.generate_key().decode("ascii")
    settings = MerchantSettings(
        database_url=TEST_DATABASE_URL,
        encryption_key=SecretStr(encryption_key),
        service_secret=SecretStr(SERVICE_SECRET),
        support_secret=SecretStr(SUPPORT_SECRET),
        session_ttl_seconds=86400,
    )
    vault = SecretVault(encryption_key)
    engine = create_engine(TEST_DATABASE_URL)
    maker = create_sessionmaker(engine)
    clock = Clock(datetime.now(UTC))
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)

    app = create_app(settings)
    app.state.sessionmaker = maker

    def service(session: Annotated[AsyncSession, Depends(get_session)]) -> MerchantService:
        return MerchantService(session, vault, session_ttl_seconds=86400, now=lambda: clock.instant)

    app.dependency_overrides[get_service] = service
    try:
        async with (
            app.router.lifespan_context(app),
            AsyncClient(
                transport=ASGITransport(app=app), base_url="http://merchant.test"
            ) as client,
        ):
            yield AccountAPI(client, maker, vault, clock)
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
        await engine.dispose()


def _registration(email: str = "first@example.com") -> dict[str, str]:
    return {
        "email": email,
        "password": PASSWORD,
        "name": "First shop",
        "provider_name": "fake",
        "provider_secret_key": PROVIDER_SECRET,
    }


async def _register(api: AccountAPI, email: str = "first@example.com") -> dict[str, Any]:
    response = await api.client.post("/merchants", json=_registration(email))
    assert response.status_code == 201, response.text
    assert response.headers["Cache-Control"] == "no-store"
    result: dict[str, Any] = response.json()
    return result


async def _login(
    api: AccountAPI, email: str = "first@example.com", password: str = PASSWORD
) -> str:
    response = await api.client.post("/auth/token", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json()["token_type"] == "bearer"
    assert response.json()["expires_in"] == 86400
    return str(response.json()["access_token"])


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _configuration(api: AccountAPI, merchant_id: str) -> dict[str, Any]:
    response = await api.client.get(
        f"/internal/merchants/{merchant_id}/configuration", headers=SERVICE_HEADERS
    )
    assert response.status_code == 200, response.text
    assert response.headers["Cache-Control"] == "no-store"
    result: dict[str, Any] = response.json()
    return result


async def test_signup_and_login_keep_credentials_private_in_storage(
    account_api: AccountAPI,
) -> None:
    profile = await _register(account_api, "First@EXAMPLE.COM")
    token = await _login(account_api, "FIRST@example.com")
    response = await account_api.client.get("/profile", headers=_bearer(token))
    assert response.status_code == 200
    assert response.json() == profile
    assert response.headers["Cache-Control"] == "no-store"
    assert profile["email"] == "first@example.com"
    assert "inn" not in profile and "ogrn" not in profile
    assert "password" not in profile and "provider_secret_key" not in profile
    assert PASSWORD not in response.text and PROVIDER_SECRET not in response.text
    assert profile["previous_api_keys"] == []

    merchant_id = UUID(profile["merchant_id"])
    async with account_api.maker() as session:
        account = await session.get(MerchantAccountModel, merchant_id)
        key = await session.get(MerchantAPIKeyModel, UUID(profile["api_key_id"]))
        credential = await session.scalar(select(MerchantProviderCredentialModel))
        login = await session.scalar(select(MerchantSessionModel))
        assert account is not None and key is not None and credential is not None and login
        assert account.password_hash.startswith("$argon2id$")
        assert account_api.vault.decrypt(account.encrypted_api_key) == profile["api_key"]
        assert account_api.vault.decrypt(credential.encrypted_secret) == PROVIDER_SECRET
        assert key.secret_digest == credential_digest(profile["api_key"])
        assert login.token_digest == credential_digest(token)
        for table in (
            MerchantAccountModel.__table__,
            MerchantAPIKeyModel.__table__,
            MerchantProviderCredentialModel.__table__,
            MerchantSessionModel.__table__,
        ):
            stored = str((await session.execute(select(table))).mappings().all())
            for secret in (PASSWORD, PROVIDER_SECRET, profile["api_key"], token):
                assert secret not in stored

    normalized_password = await account_api.client.post(
        "/auth/token", json={"email": profile["email"], "password": PASSWORD.strip()}
    )
    assert normalized_password.status_code == 401


async def test_normalized_duplicate_email_rolls_back_entire_registration(
    account_api: AccountAPI,
) -> None:
    await _register(account_api)
    response = await account_api.client.post("/merchants", json=_registration("FIRST@EXAMPLE.COM"))
    assert response.status_code == 409
    assert PASSWORD not in response.text and PROVIDER_SECRET not in response.text
    async with account_api.maker() as session:
        assert await session.scalar(select(func.count()).select_from(MerchantModel)) == 1
        assert await session.scalar(select(func.count()).select_from(MerchantAccountModel)) == 1
        assert await session.scalar(select(func.count()).select_from(MerchantAPIKeyModel)) == 1


async def test_concurrent_signup_of_normalized_email_creates_exactly_one_account(
    account_api: AccountAPI,
) -> None:
    outcomes: list[int] = []

    async def register(email: str) -> None:
        response = await account_api.client.post("/merchants", json=_registration(email))
        outcomes.append(response.status_code)

    async with anyio.create_task_group() as group:
        group.start_soon(register, "Concurrent@example.com")
        group.start_soon(register, "CONCURRENT@EXAMPLE.COM")
    assert sorted(outcomes) == [201, 409]
    async with account_api.maker() as session:
        for model in (
            MerchantModel,
            MerchantAccountModel,
            MerchantAPIKeyModel,
            MerchantProviderCredentialModel,
        ):
            assert await session.scalar(select(func.count()).select_from(model)) == 1


async def test_profile_identity_is_bound_to_session_and_spoofing_cannot_change_other_account(
    account_api: AccountAPI,
) -> None:
    first = await _register(account_api)
    second = await _register(account_api, "second@example.com")
    first_token = await _login(account_api)
    second_token = await _login(account_api, "second@example.com")
    response = await account_api.client.get(
        "/profile",
        params={"merchant_id": second["merchant_id"]},
        headers={**_bearer(first_token), "X-Merchant-ID": second["merchant_id"]},
    )
    assert response.json()["merchant_id"] == first["merchant_id"]
    rejected = await account_api.client.patch(
        "/profile",
        json={"merchant_id": second["merchant_id"], "name": "Attacker supplied owner"},
        headers=_bearer(first_token),
    )
    assert rejected.status_code == 422
    updated = await account_api.client.patch(
        "/profile",
        json={"name": "First renamed"},
        headers={**_bearer(first_token), "X-Merchant-ID": second["merchant_id"]},
    )
    assert updated.status_code == 200
    other = await account_api.client.get("/profile", headers=_bearer(second_token))
    assert other.json()["name"] == second["name"]
    assert other.json()["api_key"] == second["api_key"]


async def test_webhook_retry_settings_and_provider_change_are_persisted(
    account_api: AccountAPI,
) -> None:
    profile = await _register(account_api)
    token = await _login(account_api)
    response = await account_api.client.patch(
        "/profile",
        json={
            "webhook_url": "https://merchant.example/events",
            "retry_max_attempts": 12,
            "retry_window_seconds": 7200,
        },
        headers=_bearer(token),
    )
    assert response.status_code == 200
    assert response.json()["webhook_url"] == "https://merchant.example/events"
    assert response.json()["retry_policy"] == {"max_attempts": 12, "window_seconds": 7200}
    response = await account_api.client.patch(
        "/profile", json={"provider_name": "next"}, headers=_bearer(token)
    )
    assert response.status_code == 422
    unchanged = await _configuration(account_api, profile["merchant_id"])
    assert unchanged["provider_name"] == "fake"
    assert unchanged["webhook_url"] == "https://merchant.example/events"
    assert unchanged["retry_policy"] == {"max_attempts": 12, "window_seconds": 7200}
    assert [item["secret_key"] for item in unchanged["credentials"]] == [PROVIDER_SECRET]
    response = await account_api.client.patch(
        "/profile",
        json={
            "webhook_url": None,
            "provider_name": "next",
            "provider_secret_key": "next-provider-secret",
        },
        headers=_bearer(token),
    )
    assert response.status_code == 200
    assert response.json()["webhook_url"] is None
    assert response.json()["provider_name"] == "next"
    assert response.json()["api_key"] == profile["api_key"]
    config = await _configuration(account_api, profile["merchant_id"])
    assert config["retry_policy"] == {"max_attempts": 12, "window_seconds": 7200}
    assert config["provider_name"] == "next"
    assert {credential["secret_key"] for credential in config["credentials"]} == {
        PROVIDER_SECRET,
        "next-provider-secret",
    }


async def test_rapid_rotations_preserve_each_previous_key_for_its_own_24_hours(
    account_api: AccountAPI,
) -> None:
    original = await _register(account_api)
    token = await _login(account_api)
    initial_time = account_api.clock.instant
    first = await account_api.client.patch(
        "/profile/secrets",
        json={"rotate_api_key": True, "provider_secret_key": "first-rotated-provider-secret"},
        headers=_bearer(token),
    )
    assert first.status_code == 200
    account_api.clock.instant += timedelta(hours=1)
    second = await account_api.client.patch(
        "/profile/secrets",
        json={"rotate_api_key": True, "provider_secret_key": "second-rotated-provider-secret"},
        headers=_bearer(token),
    )
    assert second.status_code == 200
    assert len({original["api_key"], first.json()["api_key"], second.json()["api_key"]}) == 3
    previous = {
        entry["api_key_id"]: datetime.fromisoformat(entry["expires_at"])
        for entry in second.json()["previous_api_keys"]
    }
    assert previous == {
        original["api_key_id"]: initial_time + timedelta(hours=24),
        first.json()["api_key_id"]: initial_time + timedelta(hours=25),
    }
    for key in (original["api_key"], first.json()["api_key"], second.json()["api_key"]):
        authentication = await account_api.client.post(
            "/internal/authenticate", json={"api_key": key}, headers=SERVICE_HEADERS
        )
        assert authentication.status_code == 200
    config = await _configuration(account_api, original["merchant_id"])
    assert len(config["credentials"]) == 3
    account_api.clock.instant = initial_time + timedelta(hours=24)
    config = await _configuration(account_api, original["merchant_id"])
    assert {item["secret_key"] for item in config["credentials"]} == {
        "first-rotated-provider-secret",
        "second-rotated-provider-secret",
    }
    for key, expected in (
        (original["api_key"], 403),
        (first.json()["api_key"], 200),
        (second.json()["api_key"], 200),
    ):
        response = await account_api.client.post(
            "/internal/authenticate", json={"api_key": key}, headers=SERVICE_HEADERS
        )
        assert response.status_code == expected
    account_api.clock.instant = initial_time + timedelta(hours=25)
    config = await _configuration(account_api, original["merchant_id"])
    assert [item["secret_key"] for item in config["credentials"]] == [
        "second-rotated-provider-secret"
    ]
    assert config["credentials"][0]["valid_until"] is None
    for key, expected in (
        (original["api_key"], 403),
        (first.json()["api_key"], 403),
        (second.json()["api_key"], 200),
    ):
        response = await account_api.client.post(
            "/internal/authenticate", json={"api_key": key}, headers=SERVICE_HEADERS
        )
        assert response.status_code == expected


async def test_concurrent_rotations_keep_both_issued_keys_and_one_current_key(
    account_api: AccountAPI,
) -> None:
    original = await _register(account_api)
    token = await _login(account_api)
    issued: list[dict[str, Any]] = []

    async def rotate(secret: str) -> None:
        response = await account_api.client.patch(
            "/profile/secrets",
            json={"rotate_api_key": True, "provider_secret_key": secret},
            headers=_bearer(token),
        )
        assert response.status_code == 200, response.text
        issued.append(response.json())

    async with anyio.create_task_group() as group:
        group.start_soon(rotate, "concurrent-provider-secret-one")
        group.start_soon(rotate, "concurrent-provider-secret-two")
    profile = await account_api.client.get("/profile", headers=_bearer(token))
    assert profile.status_code == 200
    assert len(profile.json()["previous_api_keys"]) == 2
    assert profile.json()["api_key"] in {item["api_key"] for item in issued}
    for key in (original["api_key"], *(item["api_key"] for item in issued)):
        response = await account_api.client.post(
            "/internal/authenticate", json={"api_key": key}, headers=SERVICE_HEADERS
        )
        assert response.status_code == 200
    config = await _configuration(account_api, original["merchant_id"])
    assert len(config["credentials"]) == 3
    assert sum(item["valid_until"] is None for item in config["credentials"]) == 1


async def test_expired_previous_api_key_is_rejected_and_current_key_stays_valid(
    account_api: AccountAPI,
) -> None:
    original = await _register(account_api)
    token = await _login(account_api)
    rotated = await account_api.client.patch(
        "/profile/secrets", json={"rotate_api_key": True}, headers=_bearer(token)
    )
    assert rotated.status_code == 200
    async with account_api.maker() as session:
        await session.execute(
            update(MerchantAPIKeyModel)
            .where(MerchantAPIKeyModel.id == UUID(original["api_key_id"]))
            .values(
                created_at=account_api.clock.instant - timedelta(days=2),
                expires_at=account_api.clock.instant - timedelta(seconds=1),
            )
        )
        await session.commit()
    for key, expected in ((original["api_key"], 403), (rotated.json()["api_key"], 200)):
        response = await account_api.client.post(
            "/internal/authenticate", json={"api_key": key}, headers=SERVICE_HEADERS
        )
        assert response.status_code == expected


async def test_privileged_routes_reject_account_and_wrong_service_credentials(
    account_api: AccountAPI,
) -> None:
    profile = await _register(account_api)
    token = await _login(account_api)
    internal_url = f"/internal/merchants/{profile['merchant_id']}/configuration"
    for headers in ({}, _bearer(token), SUPPORT_HEADERS, {"X-Service-Secret": "wrong"}):
        config = await account_api.client.get(internal_url, headers=headers)
        verification = await account_api.client.post(
            "/internal/authenticate", json={"api_key": profile["api_key"]}, headers=headers
        )
        assert config.status_code == verification.status_code == 403
        assert PROVIDER_SECRET not in config.text
    for headers in ({}, _bearer(token), SERVICE_HEADERS, {"X-Support-Secret": "wrong"}):
        deletion = await account_api.client.delete(
            "/profile", params={"merchant_id": profile["merchant_id"]}, headers=headers
        )
        assert deletion.status_code == 403
    assert (await account_api.client.get("/profile", headers=_bearer(token))).status_code == 200


async def test_support_soft_delete_revokes_access_but_retains_financial_operations(
    account_api: AccountAPI,
) -> None:
    profile = await _register(account_api)
    token = await _login(account_api)
    merchant_id = UUID(profile["merchant_id"])
    payment_id = new_uuid()
    async with account_api.maker() as session:
        session.add(
            PaymentModel(
                id=payment_id,
                merchant_id=merchant_id,
                provider_id=1,
                amount=Decimal("100.00"),
                currency="RUB",
                status=PaymentStatuses.PROCESSING,
            )
        )
        await session.commit()
    deletion = await account_api.client.delete(
        "/profile", params={"merchant_id": str(merchant_id)}, headers=SUPPORT_HEADERS
    )
    assert deletion.status_code == 204
    assert deletion.content == b""
    assert (await account_api.client.get("/profile", headers=_bearer(token))).status_code == 401
    login = await account_api.client.post(
        "/auth/token", json={"email": profile["email"], "password": PASSWORD}
    )
    assert login.status_code == 401
    authentication = await account_api.client.post(
        "/internal/authenticate", json={"api_key": profile["api_key"]}, headers=SERVICE_HEADERS
    )
    assert authentication.status_code == 403
    config = await _configuration(account_api, str(merchant_id))
    assert config["is_active"] is False
    assert config["credentials"][0]["secret_key"] == PROVIDER_SECRET
    async with account_api.maker() as session:
        merchant = await session.get(MerchantModel, merchant_id)
        assert merchant is not None and not merchant.is_active and merchant.deleted_at is not None
        deleted_at = merchant.deleted_at
        callback = ProcessProviderCallbackUseCase(SystemSQLAlchemyPaymentRepository(session))
        result = await callback(ProviderCallbackInputDTO(payment_id=payment_id, status="success"))
        await session.commit()
        assert result.status == PaymentStatuses.SUCCESS
        payment = await session.get(PaymentModel, payment_id)
        assert payment is not None and payment.amount == Decimal("100.00")
        with pytest.raises(ValueError, match="not found"):
            await SQLAlchemyMerchantRepository(session).set_active(merchant_id, active=True)
    repeated = await account_api.client.delete(
        "/profile", params={"merchant_id": str(merchant_id)}, headers=SUPPORT_HEADERS
    )
    assert repeated.status_code == 204
    async with account_api.maker() as session:
        assert (
            await session.scalar(
                select(MerchantModel.deleted_at).where(MerchantModel.id == merchant_id)
            )
            == deleted_at
        )


async def test_password_rotation_revokes_all_sessions_without_revoking_machine_key(
    account_api: AccountAPI,
) -> None:
    profile = await _register(account_api)
    first_token = await _login(account_api)
    second_token = await _login(account_api)
    new_password = " rotated account password "
    changed = await account_api.client.patch(
        "/profile/secrets", json={"password": new_password}, headers=_bearer(first_token)
    )
    assert changed.status_code == 200
    assert new_password not in changed.text
    for token in (first_token, second_token):
        response = await account_api.client.get("/profile", headers=_bearer(token))
        assert response.status_code == 401
    old_password = await account_api.client.post(
        "/auth/token", json={"email": profile["email"], "password": PASSWORD}
    )
    assert old_password.status_code == 401
    await _login(account_api, password=new_password)
    machine = await account_api.client.post(
        "/internal/authenticate", json={"api_key": profile["api_key"]}, headers=SERVICE_HEADERS
    )
    assert machine.status_code == 200


async def test_missing_invalid_and_expired_sessions_have_same_public_error(
    account_api: AccountAPI,
) -> None:
    profile = await _register(account_api)
    token = await _login(account_api)
    account_api.clock.instant += timedelta(days=1)
    expected = {"detail": "Invalid or expired access token"}
    for headers in (
        {},
        _bearer("bad"),
        _bearer("ms_" + "x" * 43),
        _bearer(profile["api_key"]),
        {"Authorization": "Basic ignored"},
        _bearer(token),
    ):
        response = await account_api.client.get("/profile", headers=headers)
        assert response.status_code == 401
        assert response.json() == expected
        assert response.headers["WWW-Authenticate"] == "Bearer"
        assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("/profile", {"retry_max_attempts": 0}),
        ("/profile", {"retry_window_seconds": 86401}),
        ("/profile", {"webhook_url": "ftp://example.com/events"}),
        ("/profile", {"webhook_url": "https://user:PRIVATE@example.com/events"}),
        ("/profile", {"provider_secret_key": "PRIVATE", "name": None}),
        ("/profile", {}),
        ("/profile/secrets", {"rotate_api_key": False}),
        ("/profile/secrets", {"password": "PRIVATE"}),
    ],
)
async def test_invalid_updates_never_echo_supplied_secrets(
    account_api: AccountAPI, path: str, payload: dict[str, Any]
) -> None:
    await _register(account_api)
    token = await _login(account_api)
    response = await account_api.client.patch(path, json=payload, headers=_bearer(token))
    assert response.status_code == 422
    assert "PRIVATE" not in response.text
    assert '"input"' not in response.text
    assert '"ctx"' not in response.text
    assert response.headers["Cache-Control"] == "no-store"


async def test_signup_has_no_tax_fields_and_validation_does_not_echo_password(
    account_api: AccountAPI,
) -> None:
    payload = {**_registration(), "password": "PRIVATE", "inn": "1234567890", "ogrn": "123"}
    response = await account_api.client.post("/merchants", json=payload)
    assert response.status_code == 422
    assert "PRIVATE" not in response.text
    assert '"input"' not in response.text
    async with account_api.maker() as session:
        assert await session.scalar(select(func.count()).select_from(MerchantModel)) == 0


async def test_health_checks_database(account_api: AccountAPI) -> None:
    response = await account_api.client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "db": 1}
