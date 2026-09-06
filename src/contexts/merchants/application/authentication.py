from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from src.contexts.merchants.application.public import MerchantIdentity
from src.contexts.merchants.domain.api_key import APIKey, credential_digest, credential_id


class CredentialRepository(Protocol):
    async def find_key(self, key_id: UUID) -> tuple[APIKey, bool] | None: ...

    async def find_legacy_key(self, digest: str) -> tuple[APIKey, bool] | None: ...


class AuthenticateAPIKey:
    def __init__(self, repository: CredentialRepository) -> None:
        self._repository = repository

    async def __call__(self, token: str | None) -> MerchantIdentity | None:
        if not token:
            return None
        digest = credential_digest(token)
        key_id = credential_id(token)
        record = await self._repository.find_key(key_id) if key_id is not None else None
        # The legacy lookup only accepts an explicitly imported database credential.
        # Even legacy strings resembling the new format keep working after import.
        if record is None or not record[0].authenticates(digest, datetime.now(UTC)):
            record = await self._repository.find_legacy_key(digest)
        if record is None:
            return None
        key, merchant_is_active = record
        if not key.authenticates(digest, datetime.now(UTC)) or not merchant_is_active:
            return None
        return MerchantIdentity(merchant_id=key.merchant_id, api_key_id=key.id)
