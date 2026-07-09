import secrets

from dishka import Provider, Scope, provide
from fastapi import HTTPException, Request, status

from src.shared.config import Settings


class Authenticated:
    """Marker of successful merchant authentication via API key."""


class CallbackAuthenticated:
    """Marker of successful provider authentication via callback secret."""


class AuthProvider(Provider):
    scope = Scope.REQUEST

    @provide
    def authenticate(self, request: Request, settings: Settings) -> Authenticated:
        api_key = request.headers.get("X-API-Key")
        # encode: compare_digest on str raises TypeError for a non-ASCII header
        # value — that must be a 401, not a 500.
        if api_key is None or not secrets.compare_digest(
            api_key.encode("utf-8"), settings.api_key.encode("utf-8")
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or missing API key",
            )
        return Authenticated()

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
