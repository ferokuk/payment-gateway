from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID as PY_UUID

from sqlalchemy import DateTime, Numeric, String, func
from sqlalchemy import Enum as SQLEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.contexts.core_payment.domain.payment import PaymentStatuses
from src.shared.database.database import Base
from src.shared.ids import new_uuid


class PaymentModel(Base):
    __tablename__ = "payments"
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
