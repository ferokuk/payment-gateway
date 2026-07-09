from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from src.contexts.core_payment.domain.statuses import PaymentStatuses


class CreatePaymentRequest(BaseModel):
    amount: Decimal = Field(gt=0, decimal_places=2, description="Payment amount")
    currency: str = Field(min_length=3, max_length=3, pattern=r"^[A-Z]{3}$", description="ISO 4217")
    provider_id: int = Field(gt=0, description="Payment provider ID")
    metadata: dict[str, Any] | None = Field(default=None, description="Arbitrary merchant data")


class PaymentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    payment_id: UUID = Field(description="ID of the created payment")
    status: PaymentStatuses = Field(description="Current payment status")
    amount: Decimal = Field(description="Payment amount")
    currency: str = Field(description="ISO 4217")
    created_at: datetime = Field(description="Creation moment in UTC")


class PaymentStatusResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    payment_id: UUID = Field(description="Payment ID")
    status: PaymentStatuses = Field(description="Current payment status")
