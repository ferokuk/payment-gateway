"""Payment authentication through the Merchant service's internal HTTP contract."""

from uuid import UUID

from httpx import AsyncClient, HTTPError
from pydantic import BaseModel, ValidationError

from src.contexts.merchants.application.public import MerchantIdentity


class MerchantServiceUnavailable(Exception):
    """The trusted Merchant service cannot currently verify credentials."""


class _AuthenticationResponse(BaseModel):
    merchant_id: UUID
    api_key_id: UUID


class HTTPMerchantAuthenticator:
    def __init__(self, client: AsyncClient, service_secret: str) -> None:
        self._client = client
        self._service_secret = service_secret

    async def __call__(self, api_key: str | None) -> MerchantIdentity | None:
        if not api_key or len(api_key) > 4096:
            return None
        if not self._service_secret:
            raise MerchantServiceUnavailable("Merchant authentication is unavailable")
        try:
            response = await self._client.post(
                "/internal/authenticate",
                json={"api_key": api_key},
                headers={"X-Service-Secret": self._service_secret},
            )
        except HTTPError:
            raise MerchantServiceUnavailable("Merchant authentication is unavailable") from None
        if response.status_code in (401, 403):
            return None
        if response.status_code != 200:
            raise MerchantServiceUnavailable("Merchant authentication is unavailable")
        try:
            identity = _AuthenticationResponse.model_validate_json(response.content)
        except ValidationError:
            raise MerchantServiceUnavailable("Merchant authentication is unavailable") from None
        return MerchantIdentity(identity.merchant_id, identity.api_key_id)
