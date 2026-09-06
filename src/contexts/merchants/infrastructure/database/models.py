from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
from src.shared.database.database import Base
from src.shared.ids import new_uuid


class MerchantModel(Base):
    __tablename__ = "merchants"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class MerchantAPIKeyModel(Base):
    __tablename__ = "merchant_api_keys"
    __table_args__ = (
        UniqueConstraint("secret_digest", name="uq_merchant_api_keys_secret_digest"),
        Index(
            "uq_merchant_api_keys_legacy",
            "is_legacy",
            unique=True,
            postgresql_where=text("is_legacy"),
        ),
        CheckConstraint(
            "expires_at IS NULL OR expires_at > created_at", name="ck_merchant_api_keys_expiry"
        ),
        CheckConstraint(
            "NOT is_legacy OR merchant_id = '00000000-0000-4000-8000-000000000001'::uuid",
            name="ck_merchant_api_keys_legacy_owner",
        ),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=new_uuid)
    merchant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("merchants.id", name="fk_merchant_api_keys_merchant_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    secret_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_legacy: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
