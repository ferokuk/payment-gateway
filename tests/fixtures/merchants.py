from typing import cast
from uuid import UUID

from dishka import Provider, Scope, provide
from fastapi import HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession
from src.contexts.merchants.application.authentication import AuthenticateAPIKey
from src.contexts.merchants.application.public import MerchantIdentity
from src.contexts.merchants.infrastructure.database.repositories import SQLAlchemyMerchantRepository
from src.shared.config import Settings
from src.shared.security import AuthProvider, CallbackAuthenticated

MERCHANT_ID = UUID("00000000-0000-4000-8000-000000000001")
OTHER_MERCHANT_ID = UUID("00000000-0000-4000-8000-000000000002")


class LocalMerchantAuthProvider(Provider):
    """Exercise legacy credential persistence in DB tests without a remote service."""

    scope = Scope.REQUEST

    @provide
    async def authenticate(self, request: Request, session: AsyncSession) -> MerchantIdentity:
        identity = await AuthenticateAPIKey(SQLAlchemyMerchantRepository(session))(
            request.headers.get("X-API-Key")
        )
        if identity is None:
            raise HTTPException(status_code=401, detail="Invalid or missing API key")
        return identity

    @provide
    def authenticate_callback(self, request: Request, settings: Settings) -> CallbackAuthenticated:
        return cast(
            "CallbackAuthenticated", AuthProvider().authenticate_callback(request, settings)
        )


async def seed_legacy_merchant(connection: AsyncConnection) -> None:
    from datetime import UTC, datetime

    from sqlalchemy import insert
    from src.contexts.merchants.domain.api_key import credential_digest
    from src.contexts.merchants.infrastructure.database.models import (
        MerchantAPIKeyModel,
        MerchantModel,
    )

    await connection.execute(insert(MerchantModel).values(id=MERCHANT_ID, name="Legacy test"))
    await connection.execute(
        insert(MerchantAPIKeyModel).values(
            id=MERCHANT_ID,
            merchant_id=MERCHANT_ID,
            secret_digest=credential_digest("test-api-key"),
            label="Test",
            created_at=datetime.now(UTC),
            is_legacy=True,
        )
    )
