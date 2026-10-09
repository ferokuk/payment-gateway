import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from src.contexts.merchants.application.authentication import AuthenticateAPIKey
from src.contexts.merchants.application.public import MerchantIdentity
from src.contexts.merchants.domain.api_key import credential_digest, issue_api_key
from src.contexts.merchants.domain.merchant_config import MerchantConfig
from src.contexts.merchants.infrastructure.database.models import (
    MerchantAccountModel,
    MerchantAPIKeyModel,
    MerchantModel,
    MerchantProviderCredentialModel,
    MerchantSessionModel,
)
from src.contexts.merchants.infrastructure.database.repositories import SQLAlchemyMerchantRepository
from src.contexts.merchants.infrastructure.secrets import (
    SecretVault,
    hash_password,
    verify_password,
)
from src.shared.ids import new_uuid

ROTATION_GRACE = timedelta(hours=24)


class MerchantNotFound(ValueError):
    pass


class MerchantConflict(ValueError):
    pass


class MerchantUnauthorized(ValueError):
    pass


class MerchantInvalidRequest(ValueError):
    pass


class MerchantService:
    """Account operations and credential lifecycle owned by the merchant service."""

    def __init__(
        self,
        session: AsyncSession,
        vault: SecretVault,
        session_ttl_seconds: int = 3600,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._session = session
        self._vault = vault
        self._session_ttl_seconds = session_ttl_seconds
        self._now = now or (lambda: datetime.now(UTC))

    async def register(
        self,
        *,
        email: str,
        name: str,
        password: str,
        provider_name: str,
        provider_secret_key: str,
    ) -> dict[str, Any]:
        password_hash = await hash_password(password)
        now = self._now()
        merchant_id = new_uuid()
        issued = issue_api_key(merchant_id, "Account API key", now)
        merchant = MerchantModel(id=merchant_id, name=name.strip(), is_active=True, created_at=now)
        account = MerchantAccountModel(
            merchant_id=merchant_id,
            email=email.strip().lower(),
            password_hash=password_hash,
            api_key_id=issued.key.id,
            encrypted_api_key=self._vault.encrypt(issued.token),
            provider_name=provider_name.strip(),
            retry_max_attempts=3,
            retry_window_seconds=60,
        )
        try:
            self._session.add(merchant)
            await self._session.flush()
            self._session.add(
                MerchantAPIKeyModel(
                    id=issued.key.id,
                    merchant_id=merchant_id,
                    secret_digest=issued.key.secret_digest,
                    label=issued.key.label,
                    created_at=now,
                )
            )
            await self._session.flush()
            self._session.add(account)
            self._session.add(
                MerchantProviderCredentialModel(
                    merchant_id=merchant_id,
                    provider_name=account.provider_name,
                    encrypted_secret=self._vault.encrypt(provider_secret_key),
                    created_at=now,
                )
            )
            await self._session.flush()
            result = await self._profile(merchant, account, now)
            # A returned key must already exist durably when the client starts using it.
            await self._session.commit()
        except IntegrityError:
            await self._session.rollback()
            raise MerchantConflict("Merchant is already registered") from None
        return result

    async def login(self, email: str, password: str) -> dict[str, Any]:
        account = await self._session.scalar(
            select(MerchantAccountModel).where(MerchantAccountModel.email == email.strip().lower())
        )
        valid = await verify_password(password, account.password_hash if account else None)
        if not valid or account is None:
            raise MerchantUnauthorized("Invalid email or password")
        merchant, account = await self._account(account.merchant_id, lock=True)
        if not merchant.is_active or merchant.deleted_at is not None:
            raise MerchantUnauthorized("Invalid email or password")
        # Re-read after locking: password rotation can finish while Argon2 is running.
        if not await verify_password(password, account.password_hash):
            raise MerchantUnauthorized("Invalid email or password")
        now = self._now()
        token = f"ms_{secrets.token_urlsafe(32)}"
        self._session.add(
            MerchantSessionModel(
                token_digest=credential_digest(token),
                merchant_id=merchant.id,
                created_at=now,
                expires_at=now + timedelta(seconds=self._session_ttl_seconds),
            )
        )
        await self._session.commit()
        return {
            "access_token": token,
            "token_type": "bearer",
            "expires_in": self._session_ttl_seconds,
        }

    async def authenticate_session(self, token: str | None) -> UUID:
        if not token or not token.startswith("ms_") or len(token) != 46:
            raise MerchantUnauthorized("Invalid or expired access token")
        merchant_id = await self._session.scalar(
            select(MerchantSessionModel.merchant_id)
            .join(MerchantModel, MerchantModel.id == MerchantSessionModel.merchant_id)
            .where(
                MerchantSessionModel.token_digest == credential_digest(token),
                MerchantSessionModel.expires_at > self._now(),
                MerchantSessionModel.revoked_at.is_(None),
                MerchantModel.is_active.is_(True),
                MerchantModel.deleted_at.is_(None),
            )
        )
        if merchant_id is None:
            raise MerchantUnauthorized("Invalid or expired access token")
        return merchant_id

    async def _account(
        self, merchant_id: UUID, *, lock: bool = False
    ) -> tuple[MerchantModel, MerchantAccountModel]:
        query = (
            select(MerchantModel, MerchantAccountModel)
            .join(MerchantAccountModel, MerchantAccountModel.merchant_id == MerchantModel.id)
            .where(MerchantModel.id == merchant_id)
            .execution_options(populate_existing=True)
        )
        if lock:
            # Serialize profile changes, rotation and deletion for one account.
            query = query.with_for_update()
        row = (await self._session.execute(query)).one_or_none()
        if row is None:
            raise MerchantNotFound("Merchant profile not found")
        return row[0], row[1]

    @staticmethod
    def _require_active(merchant: MerchantModel) -> None:
        if not merchant.is_active or merchant.deleted_at is not None:
            raise MerchantUnauthorized("Merchant account is unavailable")

    async def _profile(
        self, merchant: MerchantModel, account: MerchantAccountModel, now: datetime
    ) -> dict[str, Any]:
        old_keys = (
            await self._session.scalars(
                select(MerchantAPIKeyModel)
                .where(
                    MerchantAPIKeyModel.merchant_id == merchant.id,
                    MerchantAPIKeyModel.id != account.api_key_id,
                    MerchantAPIKeyModel.expires_at > now,
                    MerchantAPIKeyModel.revoked_at.is_(None),
                )
                .order_by(MerchantAPIKeyModel.created_at, MerchantAPIKeyModel.id)
            )
        ).all()
        return {
            "merchant_id": merchant.id,
            "email": account.email,
            "name": merchant.name,
            "provider_name": account.provider_name,
            "webhook_url": account.webhook_url,
            "retry_policy": {
                "max_attempts": account.retry_max_attempts,
                "window_seconds": account.retry_window_seconds,
            },
            "api_key": self._vault.decrypt(account.encrypted_api_key),
            "api_key_id": account.api_key_id,
            "previous_api_keys": [
                {"api_key_id": key.id, "expires_at": key.expires_at} for key in old_keys
            ],
            "created_at": merchant.created_at,
        }

    async def get_profile(self, merchant_id: UUID) -> dict[str, Any]:
        merchant, account = await self._account(merchant_id)
        self._require_active(merchant)
        return await self._profile(merchant, account, self._now())

    async def update_profile(self, merchant_id: UUID, changes: dict[str, Any]) -> dict[str, Any]:
        merchant, account = await self._account(merchant_id, lock=True)
        self._require_active(merchant)
        provider_name = changes.get("provider_name", account.provider_name)
        provider_secret = changes.get("provider_secret_key")
        if provider_name != account.provider_name and not provider_secret:
            raise MerchantInvalidRequest("Changing provider requires its new secret key")
        try:
            config = MerchantConfig(
                provider_name=provider_name,
                webhook_url=changes.get("webhook_url", account.webhook_url),
                retry_max_attempts=changes.get("retry_max_attempts", account.retry_max_attempts),
                retry_window_seconds=changes.get(
                    "retry_window_seconds", account.retry_window_seconds
                ),
            )
        except ValueError as error:
            raise MerchantInvalidRequest(str(error)) from None
        now = self._now()
        if provider_secret is not None:
            await self._rotate_provider(merchant.id, config.provider_name, provider_secret, now)
        if "name" in changes:
            merchant.name = changes["name"]
        account.provider_name = config.provider_name
        account.webhook_url = config.webhook_url
        account.retry_max_attempts = config.retry_max_attempts
        account.retry_window_seconds = config.retry_window_seconds
        await self._session.flush()
        result = await self._profile(merchant, account, now)
        await self._session.commit()
        return result

    async def _rotate_provider(
        self, merchant_id: UUID, provider_name: str, secret: str, now: datetime
    ) -> None:
        await self._session.execute(
            update(MerchantProviderCredentialModel)
            .where(
                MerchantProviderCredentialModel.merchant_id == merchant_id,
                MerchantProviderCredentialModel.valid_until.is_(None),
            )
            .values(valid_until=now + ROTATION_GRACE)
        )
        # Keep each older version until its own deadline, including rapid rotations.
        self._session.add(
            MerchantProviderCredentialModel(
                merchant_id=merchant_id,
                provider_name=provider_name,
                encrypted_secret=self._vault.encrypt(secret),
                created_at=now,
            )
        )

    async def rotate_secrets(
        self,
        merchant_id: UUID,
        *,
        rotate_api_key: bool = False,
        provider_secret_key: str | None = None,
        provider_name: str | None = None,
        password: str | None = None,
    ) -> dict[str, Any]:
        if not rotate_api_key and provider_secret_key is None and password is None:
            raise MerchantInvalidRequest("Select at least one credential to change")
        if provider_name is not None and provider_secret_key is None:
            raise MerchantInvalidRequest("Changing provider requires its new secret key")
        password_hash = await hash_password(password) if password is not None else None
        merchant, account = await self._account(merchant_id, lock=True)
        self._require_active(merchant)
        now = self._now()
        if rotate_api_key:
            await self._session.execute(
                update(MerchantAPIKeyModel)
                .where(
                    MerchantAPIKeyModel.id == account.api_key_id,
                    MerchantAPIKeyModel.merchant_id == merchant_id,
                )
                .values(expires_at=now + ROTATION_GRACE)
            )
            issued = issue_api_key(merchant_id, "Account API key", now)
            self._session.add(
                MerchantAPIKeyModel(
                    id=issued.key.id,
                    merchant_id=merchant_id,
                    secret_digest=issued.key.secret_digest,
                    label=issued.key.label,
                    created_at=now,
                )
            )
            await self._session.flush()
            account.api_key_id = issued.key.id
            account.encrypted_api_key = self._vault.encrypt(issued.token)
        if provider_secret_key is not None:
            selected_provider = provider_name or account.provider_name
            await self._rotate_provider(merchant_id, selected_provider, provider_secret_key, now)
            account.provider_name = selected_provider
        if password_hash is not None:
            account.password_hash = password_hash
            await self._session.execute(
                update(MerchantSessionModel)
                .where(
                    MerchantSessionModel.merchant_id == merchant_id,
                    MerchantSessionModel.revoked_at.is_(None),
                )
                .values(revoked_at=now)
            )
        await self._session.flush()
        result = await self._profile(merchant, account, now)
        await self._session.commit()
        return result

    async def delete(self, merchant_id: UUID) -> None:
        merchant = await self._session.scalar(
            select(MerchantModel).where(MerchantModel.id == merchant_id).with_for_update()
        )
        if merchant is None:
            raise MerchantNotFound("Merchant not found")
        now = self._now()
        if merchant.deleted_at is None:
            merchant.deleted_at = now
        merchant.is_active = False
        await self._session.execute(
            update(MerchantSessionModel)
            .where(
                MerchantSessionModel.merchant_id == merchant_id,
                MerchantSessionModel.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )
        await self._session.execute(
            update(MerchantAPIKeyModel)
            .where(
                MerchantAPIKeyModel.merchant_id == merchant_id,
                MerchantAPIKeyModel.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )
        await self._session.commit()

    async def authenticate_api_key(self, token: str | None) -> MerchantIdentity | None:
        return await AuthenticateAPIKey(SQLAlchemyMerchantRepository(self._session), now=self._now)(
            token
        )

    async def get_configuration(self, merchant_id: UUID) -> dict[str, Any]:
        merchant, account = await self._account(merchant_id)
        now = self._now()
        credentials = (
            await self._session.scalars(
                select(MerchantProviderCredentialModel)
                .where(
                    MerchantProviderCredentialModel.merchant_id == merchant_id,
                    (MerchantProviderCredentialModel.valid_until.is_(None))
                    | (MerchantProviderCredentialModel.valid_until > now),
                )
                .order_by(
                    MerchantProviderCredentialModel.created_at.desc(),
                    MerchantProviderCredentialModel.id.desc(),
                )
            )
        ).all()
        # Trusted workers need credentials even after closure to settle accepted operations.
        return {
            "merchant_id": merchant.id,
            "is_active": merchant.is_active and merchant.deleted_at is None,
            "provider_name": account.provider_name,
            "credentials": [
                {
                    "provider_name": credential.provider_name,
                    "secret_key": self._vault.decrypt(credential.encrypted_secret),
                    "valid_until": credential.valid_until,
                }
                for credential in credentials
            ],
            "webhook_url": account.webhook_url,
            "retry_policy": {
                "max_attempts": account.retry_max_attempts,
                "window_seconds": account.retry_window_seconds,
            },
        }
