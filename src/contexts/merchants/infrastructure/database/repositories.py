from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from src.contexts.merchants.domain.api_key import (
    APIKey,
    IssuedAPIKey,
    credential_digest,
    issue_api_key,
    validate_expiration,
)
from src.contexts.merchants.domain.merchant import LEGACY_MERCHANT_ID, Merchant
from src.contexts.merchants.infrastructure.database.models import MerchantAPIKeyModel, MerchantModel


def _key(model: MerchantAPIKeyModel) -> APIKey:
    return APIKey(
        id=model.id,
        merchant_id=model.merchant_id,
        secret_digest=model.secret_digest,
        label=model.label,
        created_at=model.created_at,
        expires_at=model.expires_at,
        revoked_at=model.revoked_at,
        is_legacy=model.is_legacy,
    )


def _label(value: str) -> str:
    value = value.strip()
    if not value or len(value) > 255:
        raise ValueError("Name/label must contain 1 to 255 characters")
    return value


class SQLAlchemyMerchantRepository:
    """Privileged merchant/credential storage; never exposed to payment use cases.

    The caller owns commit. In particular, CLI issuance prints only after commit.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_key(self, key_id: UUID) -> tuple[APIKey, bool] | None:
        result = await self._session.execute(
            select(MerchantAPIKeyModel, MerchantModel.is_active)
            .join(MerchantModel, MerchantModel.id == MerchantAPIKeyModel.merchant_id)
            .where(MerchantAPIKeyModel.id == key_id, MerchantModel.deleted_at.is_(None))
            .execution_options(populate_existing=True)
        )
        row = result.one_or_none()
        return (_key(row[0]), row[1]) if row else None

    async def find_legacy_key(self, digest: str) -> tuple[APIKey, bool] | None:
        result = await self._session.execute(
            select(MerchantAPIKeyModel, MerchantModel.is_active)
            .join(MerchantModel, MerchantModel.id == MerchantAPIKeyModel.merchant_id)
            .where(
                MerchantAPIKeyModel.secret_digest == digest,
                MerchantAPIKeyModel.is_legacy.is_(True),
                MerchantModel.deleted_at.is_(None),
            )
            .execution_options(populate_existing=True)
        )
        row = result.one_or_none()
        return (_key(row[0]), row[1]) if row else None

    async def create(self, name: str) -> Merchant:
        merchant = Merchant(
            id=uuid4(), name=_label(name), is_active=True, created_at=datetime.now(UTC)
        )
        self._session.add(
            MerchantModel(
                id=merchant.id,
                name=merchant.name,
                is_active=merchant.is_active,
                created_at=merchant.created_at,
            )
        )
        await self._session.flush()
        return merchant

    async def issue_key(
        self, merchant_id: UUID, label: str, expires_at: datetime | None = None
    ) -> IssuedAPIKey:
        merchant = await self._session.get(MerchantModel, merchant_id)
        if merchant is None:
            raise ValueError("Merchant not found")
        if not merchant.is_active or merchant.deleted_at is not None:
            raise ValueError("Merchant is inactive")
        issued = issue_api_key(merchant_id, _label(label), datetime.now(UTC), expires_at)
        await self._insert_key(issued.key)
        return issued

    async def import_legacy_key(
        self, token: str, label: str, expires_at: datetime | None = None
    ) -> APIKey:
        if not token:
            raise ValueError("Legacy credential must not be empty")
        merchant = await self._session.get(MerchantModel, LEGACY_MERCHANT_ID)
        if merchant is None:
            raise ValueError("Legacy merchant not found; apply the merchant isolation migration")
        if not merchant.is_active or merchant.deleted_at is not None:
            raise ValueError("Merchant is inactive")
        exists = await self._session.scalar(
            select(MerchantAPIKeyModel.id).where(MerchantAPIKeyModel.is_legacy.is_(True))
        )
        if exists is not None:
            raise ValueError("The legacy credential was already imported")
        now = datetime.now(UTC)
        validate_expiration(now, expires_at)
        key = APIKey(
            id=uuid4(),
            merchant_id=LEGACY_MERCHANT_ID,
            secret_digest=credential_digest(token),
            label=_label(label),
            created_at=now,
            expires_at=expires_at,
            is_legacy=True,
        )
        await self._insert_key(key)
        return key

    async def _insert_key(self, key: APIKey) -> None:
        self._session.add(
            MerchantAPIKeyModel(
                id=key.id,
                merchant_id=key.merchant_id,
                secret_digest=key.secret_digest,
                label=key.label,
                created_at=key.created_at,
                expires_at=key.expires_at,
                revoked_at=key.revoked_at,
                is_legacy=key.is_legacy,
            )
        )
        await self._session.flush()

    async def revoke_key(self, key_id: UUID) -> None:
        result = await self._session.scalar(
            update(MerchantAPIKeyModel)
            .where(MerchantAPIKeyModel.id == key_id)
            .values(revoked_at=func.coalesce(MerchantAPIKeyModel.revoked_at, datetime.now(UTC)))
            .returning(MerchantAPIKeyModel.id)
        )
        if result is None:
            raise ValueError("API key not found")

    async def set_active(self, merchant_id: UUID, *, active: bool) -> None:
        # Support closure is permanent; the maintenance CLI must not resurrect accounts.
        result = await self._session.scalar(
            update(MerchantModel)
            .where(MerchantModel.id == merchant_id, MerchantModel.deleted_at.is_(None))
            .values(is_active=active)
            .returning(MerchantModel.id)
        )
        if result is None:
            raise ValueError("Merchant not found")
