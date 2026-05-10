from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

from sqlalchemy import Column, Numeric, String, Enum as SQLEnum, DateTime, func
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.contexts.core_payment.domain.payment import PaymentStatus
from src.shared.database.database import Base


class PaymentModel(Base):
    __tablename__ = 'payments'
    id: Mapped[UUID] = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    amount: Mapped[Decimal] = Column(Numeric(20, 2), nullable=False)
    provider_id: Mapped[int] = mapped_column(nullable=False)
    currency: Mapped[str] = Column(String(3), nullable=False)
    status: Mapped[PaymentStatus] = mapped_column(
        SQLEnum(PaymentStatus, nullable=False, name='payment_status'),
        nullable=False,
    )
    created_at: Mapped[datetime] = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    meta: Mapped[dict[str, Any] | None] = mapped_column(
        "metadata", JSONB, nullable=True
    )
