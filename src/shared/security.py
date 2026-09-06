import secrets
from collections.abc import AsyncIterator

from dishka import Provider, Scope, provide
from fastapi import HTTPException, Request, status
from httpx import AsyncClient

from src.contexts.merchants.application.public import MerchantIdentity
from src.shared.config import Settings
from src.shared.merchant_client import HTTPMerchantAuthenticator, MerchantServiceUnavailable

Authenticated = MerchantIdentity


class CallbackAuthenticated:
    """Marker of successful provider authentication via callback secret."""


class AuthProvider(Provider):
    scope = Scope.REQUEST

    @provide(scope=Scope.APP)
    async def merchant_authenticator(
        self, settings: Settings
    ) -> AsyncIterator[HTTPMerchantAuthenticator]:
        async with AsyncClient(
            base_url=settings.merchant_service_url, timeout=5.0, follow_redirects=False
        ) as client:
            yield HTTPMerchantAuthenticator(client, settings.merchant_service_secret)

    @provide
    async def authenticate(
        self, request: Request, authenticator: HTTPMerchantAuthenticator
    ) -> MerchantIdentity:
        api_key = request.headers.get("X-API-Key")
        try:
            identity = await authenticator(api_key)
        except MerchantServiceUnavailable:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Merchant authentication is unavailable",
            ) from None
        if identity is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or missing API key",
            )
        return identity

    @provide
    def authenticate_callback(self, request: Request, settings: Settings) -> CallbackAuthenticated:
        secret = request.headers.get("X-Callback-Secret")
        if secret is None or not secrets.compare_digest(
            secret.encode("utf-8"), settings.callback_secret.encode("utf-8")
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or missing callback secret",
            )
        return CallbackAuthenticated()
