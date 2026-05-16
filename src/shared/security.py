import secrets

from dishka import Provider, Scope, provide
from fastapi import HTTPException, Request, status

from src.shared.config import Settings


class Authenticated:
    """Маркер успешной аутентификации мерчанта по API-ключу."""


class AuthProvider(Provider):
    scope = Scope.REQUEST

    @provide
    def authenticate(self, request: Request, settings: Settings) -> Authenticated:
        api_key = request.headers.get("X-API-Key")
        if api_key is None or not secrets.compare_digest(api_key, settings.api_key):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or missing API key",
            )
        return Authenticated()
