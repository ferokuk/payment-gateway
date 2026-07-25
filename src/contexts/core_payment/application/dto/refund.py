from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import UUID7, BaseModel, Field

from src.contexts.core_payment.domain.statuses import RefundFailureReasons, RefundStatuses

# Statuses a provider may send in a refund callback. A subset of
# RefundStatuses: created/pending cannot arrive from outside.
RefundCallbackStatus = Literal["success", "failed", "error"]


class CreateRefundInputDTO(BaseModel):
    payment_id: UUID = Field(description="ID of the payment to refund")
    amount: Decimal = Field(gt=0, decimal_places=2, description="Refund amount")
    metadata: dict[str, Any] | None = Field(default=None, description="Arbitrary merchant data")


class CreateRefundOutputDTO(BaseModel):
    refund_id: UUID7 = Field(description="ID of the created refund")
    payment_id: UUID7 = Field(description="ID of the refunded payment")
    status: RefundStatuses = Field(description="Current refund status")
    amount: Decimal = Field(description="Refund amount")
    replayed: bool = Field(
        default=False,
        exclude=True,
        description="Response replayed via Idempotency-Key (not serialized into the body)",
    )


class GetRefundStatusOutputDTO(BaseModel):
    refund_id: UUID7 = Field(description="Refund ID")
    payment_id: UUID7 = Field(description="Refunded payment ID")
    status: RefundStatuses = Field(description="Current refund status")
    amount: Decimal = Field(description="Refund amount")


class ReconciliationReportDTO(BaseModel):
    """Outcome of one reconciliation pass over refunds stuck in CREATED."""

    resumed: int = Field(default=0, description="Re-initiated at the provider, moved to pending")
    closed: int = Field(default=0, description="Provider refused it; reservation released")
    unresolved: int = Field(default=0, description="Outcome still unknown; retried next pass")
    conflicts: int = Field(default=0, description="Someone else finished the refund first")
    abandoned: int = Field(default=0, description="Too old to re-initiate safely; needs a human")


class RefundCallbackInputDTO(BaseModel):
    refund_id: UUID = Field(description="Refund ID in our system")
    status: RefundCallbackStatus = Field(description="New status from the provider")
    failure_reason: RefundFailureReasons | None = Field(
        default=None, description="Failure reason (only for failed)"
    )
    error_message: str | None = Field(default=None, description="Error message (only for error)")
