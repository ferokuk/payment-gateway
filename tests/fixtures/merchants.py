from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncConnection

MERCHANT_ID = UUID("00000000-0000-4000-8000-000000000001")
OTHER_MERCHANT_ID = UUID("00000000-0000-4000-8000-000000000002")


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
