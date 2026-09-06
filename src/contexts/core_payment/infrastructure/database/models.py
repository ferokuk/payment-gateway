from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID as PY_UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Numeric, String, func, text
from sqlalchemy import Enum as SQLEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.contexts.core_payment.domain.statuses import (
    FailureReasons,
    PaymentStatuses,
    RefundFailureReasons,
    RefundStatuses,
)
from src.shared.database.database import Base
from src.shared.ids import new_uuid


class PaymentModel(Base):
    __tablename__ = "payments"

    __table_args__ = (
        # The database as the last line of defence against an application bug
        # in the reserve/release statements.
        CheckConstraint(
            "refunded_amount >= 0 AND refunded_amount <= amount",
            name="ck_payments_refunded_amount_within_amount",
        ),
    )

    id: Mapped[PY_UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=new_uuid)
    amount: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    provider_id: Mapped[int] = mapped_column(nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    status: Mapped[PaymentStatuses] = mapped_column(
        SQLEnum(PaymentStatuses, nullable=False, name="payment_status"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    meta: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSONB, nullable=True)
    failure_reason: Mapped[FailureReasons | None] = mapped_column(
        SQLEnum(FailureReasons, nullable=True), default=None, nullable=True
    )
    error_message: Mapped[str | None] = mapped_column(default=None, nullable=True)
    refunded_amount: Mapped[Decimal] = mapped_column(
        Numeric(20, 2), nullable=False, default=Decimal("0"), server_default=text("0")
    )


class IdempotencyKeyModel(Base):
    __tablename__ = "idempotency_keys"

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payment_id: Mapped[PY_UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("payments.id"), nullable=False
    )
    # NULL <=> initiation not confirmed (the payment is stuck in CREATED).
    response_body: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class RefundModel(Base):
    __tablename__ = "refunds"

    __table_args__ = (
        CheckConstraint("reconciliation_attempts >= 0", name="ck_refunds_reconciliation_attempts"),
        Index(
            "ix_refunds_reconciliation_due",
            text("coalesce(next_reconcile_at, created_at)"),
            "id",
            postgresql_where=text("status IN ('CREATED', 'PENDING', 'ERROR')"),
        ),
    )

    id: Mapped[PY_UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=new_uuid)
    payment_id: Mapped[PY_UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("payments.id"), nullable=False, index=True
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    status: Mapped[RefundStatuses] = mapped_column(
        SQLEnum(RefundStatuses, name="refund_status"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    meta: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSONB, nullable=True)
    failure_reason: Mapped[RefundFailureReasons | None] = mapped_column(
        SQLEnum(RefundFailureReasons, nullable=True), default=None, nullable=True
    )
    error_message: Mapped[str | None] = mapped_column(default=None, nullable=True)

    # Operational scheduling stays out of the domain aggregate and public DTOs.
    next_reconcile_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reconciliation_attempts: Mapped[int] = mapped_column(nullable=False, server_default=text("0"))


class RefundIdempotencyKeyModel(Base):
    __tablename__ = "refund_idempotency_keys"

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    refund_id: Mapped[PY_UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("refunds.id"), nullable=False
    )
    # Missing HTTP snapshot: reconciliation may already have advanced the refund.
    response_body: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
