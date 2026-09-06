import secrets

from dishka import Provider, Scope, provide
from fastapi import HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.contexts.merchants.application.authentication import AuthenticateAPIKey
from src.contexts.merchants.application.public import MerchantIdentity
from src.contexts.merchants.infrastructure.database.repositories import SQLAlchemyMerchantRepository
from src.shared.config import Settings

Authenticated = MerchantIdentity


class CallbackAuthenticated:
    """Marker of successful provider authentication via callback secret."""


class AuthProvider(Provider):
    scope = Scope.REQUEST

    @provide
    async def authenticate(self, request: Request, session: AsyncSession) -> MerchantIdentity:
        api_key = request.headers.get("X-API-Key")
        identity = await AuthenticateAPIKey(SQLAlchemyMerchantRepository(session))(api_key)
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
