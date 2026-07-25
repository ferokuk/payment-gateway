from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import UUID7, BaseModel, Field

from src.contexts.core_payment.domain.exceptions import (
    InvalidRefundFailureReasonError,
    InvalidRefundStatusTransitionError,
)
from src.contexts.core_payment.domain.statuses import RefundFailureReasons, RefundStatuses


class Refund(BaseModel):
    id: UUID7 = Field(description="ID of the refund")
    payment_id: UUID7 = Field(description="ID of the refunded payment")
    amount: Decimal = Field(description="Refund amount")
    status: RefundStatuses = Field(description="Current refund status")
    created_at: datetime = Field(description="Creation moment in UTC")
    metadata: dict[str, Any] | None = Field(default=None, description="Arbitrary merchant data")
    failure_reason: RefundFailureReasons | None = Field(default=None, description="Failure reason")
    error_message: str | None = Field(default=None, description="Error message")

    def mark_pending(self) -> None:
        if RefundStatuses.PENDING not in _ALLOWED_TRANSITIONS[self.status]:
            raise InvalidRefundStatusTransitionError(
                from_status=self.status, to_status=RefundStatuses.PENDING
            )
        self.status = RefundStatuses.PENDING

    def mark_success(self) -> None:
        if RefundStatuses.SUCCESS not in _ALLOWED_TRANSITIONS[self.status]:
            raise InvalidRefundStatusTransitionError(
                from_status=self.status, to_status=RefundStatuses.SUCCESS
            )
        self.status = RefundStatuses.SUCCESS

    def mark_failed(self, reason: RefundFailureReasons) -> None:
        if RefundStatuses.FAILED not in _ALLOWED_TRANSITIONS[self.status]:
            raise InvalidRefundStatusTransitionError(
                from_status=self.status, to_status=RefundStatuses.FAILED
            )
        if reason not in _ALLOWED_FAILURE_REASONS[self.status]:
            raise InvalidRefundFailureReasonError(reason=reason, status=self.status)
        self.status = RefundStatuses.FAILED
        self.failure_reason = reason

    def mark_error(self, error_message: str) -> None:
        if RefundStatuses.ERROR not in _ALLOWED_TRANSITIONS[self.status]:
            raise InvalidRefundStatusTransitionError(
                from_status=self.status, to_status=RefundStatuses.ERROR
            )
        self.status = RefundStatuses.ERROR
        self.error_message = error_message


# There is deliberately no PROCESSING: at real PSPs a refund is a single-step
# operation (Stripe: pending -> succeeded/failed).
_ALLOWED_TRANSITIONS: dict[RefundStatuses, frozenset[RefundStatuses]] = {
    # CREATED -> FAILED is the reconciliation exit: a refund the provider
    # refused to take must be closable, otherwise the amount it reserved on the
    # payment stays locked forever.
    RefundStatuses.CREATED: frozenset({RefundStatuses.PENDING, RefundStatuses.FAILED}),
    RefundStatuses.PENDING: frozenset(
        {RefundStatuses.SUCCESS, RefundStatuses.FAILED, RefundStatuses.ERROR}
    ),
    RefundStatuses.SUCCESS: frozenset(),
    RefundStatuses.FAILED: frozenset(),
    RefundStatuses.ERROR: frozenset(),
}

_ALLOWED_FAILURE_REASONS: dict[RefundStatuses, frozenset[RefundFailureReasons]] = {
    # The reasons split by who is speaking: from CREATED the only voice is
    # "the provider did not take it", from PENDING — the provider's verdict on
    # a refund it did take.
    RefundStatuses.CREATED: frozenset({RefundFailureReasons.NOT_ACCEPTED_BY_PROVIDER}),
    RefundStatuses.PENDING: frozenset(
        {
            RefundFailureReasons.TIMEOUT,
            RefundFailureReasons.CARD_UNAVAILABLE,
            RefundFailureReasons.INSUFFICIENT_MERCHANT_BALANCE,
        }
    ),
}
