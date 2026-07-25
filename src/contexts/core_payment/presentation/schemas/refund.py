from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from src.contexts.core_payment.domain.statuses import RefundStatuses


class CreateRefundRequest(BaseModel):
    amount: Decimal = Field(gt=0, decimal_places=2, description="Refund amount")
    metadata: dict[str, Any] | None = Field(default=None, description="Arbitrary merchant data")


class RefundResponse(BaseModel):
    """Shared by POST /payments/{id}/refunds, GET /refunds/{id} and the refund
    callback: all three return the same body, so one schema instead of a mirror
    of the two payment schemas."""

    model_config = ConfigDict(from_attributes=True)

    refund_id: UUID = Field(description="Refund ID")
    payment_id: UUID = Field(description="Refunded payment ID")
    status: RefundStatuses = Field(description="Current refund status")
    amount: Decimal = Field(description="Refund amount")


class RefundListResponse(BaseModel):
    """An object rather than a bare array: a top-level list freezes the contract,
    while a field can be joined later by a cursor or a counter without breaking
    clients that already read `refunds`."""

    refunds: list[RefundResponse] = Field(description="Refunds of the payment, oldest first")
