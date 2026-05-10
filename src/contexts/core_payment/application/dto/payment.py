from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from src.contexts.core_payment.domain.payment import PaymentStatus


@dataclass(frozen=True, slots=True)
class CreatePaymentCommand:
    amount: Decimal
    currency: str
    provider_id: int
    metadata: dict[str, Any] | None


@dataclass(frozen=True, slots=True)
class CreatePaymentResult:
    payment_id: UUID
    status: PaymentStatus
    amount: Decimal
    currency: str
    created_at: datetime
