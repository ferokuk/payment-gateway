from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
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
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


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


class MerchantAccountModel(Base):
    __tablename__ = "merchant_accounts"
    __table_args__ = (
        UniqueConstraint("email", name="uq_merchant_accounts_email"),
        CheckConstraint(
            "retry_max_attempts BETWEEN 1 AND 100", name="ck_merchant_accounts_retry_max_attempts"
        ),
        CheckConstraint(
            "retry_window_seconds BETWEEN 1 AND 86400",
            name="ck_merchant_accounts_retry_window_seconds",
        ),
    )

    merchant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("merchants.id", name="fk_merchant_accounts_merchant_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    api_key_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey(
            "merchant_api_keys.id", name="fk_merchant_accounts_api_key_id", ondelete="RESTRICT"
        ),
        nullable=False,
    )
    encrypted_api_key: Mapped[str] = mapped_column(Text, nullable=False)
    provider_name: Mapped[str] = mapped_column(String(100), nullable=False)
    webhook_url: Mapped[str | None] = mapped_column(String(2048))
    retry_max_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("3")
    )
    retry_window_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("60")
    )


class MerchantProviderCredentialModel(Base):
    __tablename__ = "merchant_provider_credentials"
    __table_args__ = (
        Index(
            "uq_merchant_provider_credentials_current",
            "merchant_id",
            unique=True,
            postgresql_where=text("valid_until IS NULL"),
        ),
        CheckConstraint(
            "valid_until IS NULL OR valid_until > created_at",
            name="ck_merchant_provider_credentials_expiry",
        ),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=new_uuid)
    merchant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey(
            "merchants.id", name="fk_merchant_provider_credentials_merchant_id", ondelete="RESTRICT"
        ),
        nullable=False,
        index=True,
    )
    provider_name: Mapped[str] = mapped_column(String(100), nullable=False)
    encrypted_secret: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MerchantSessionModel(Base):
    __tablename__ = "merchant_sessions"
    __table_args__ = (
        CheckConstraint("expires_at > created_at", name="ck_merchant_sessions_expiry"),
    )

    token_digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    merchant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("merchants.id", name="fk_merchant_sessions_merchant_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
