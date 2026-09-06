from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import UUID7, BaseModel, Field

from src.contexts.core_payment.domain.exceptions import (
    InvalidPaymentFailureReasonError,
    InvalidPaymentStatusTransitionError,
)
from src.contexts.core_payment.domain.statuses import FailureReasons, PaymentStatuses


class Payment(BaseModel):
    id: UUID7 = Field(description="ID of the created payment")
    merchant_id: UUID = Field(frozen=True, description="Merchant that owns the payment")
    provider_id: int = Field(gt=0, description="Provider ID")
    status: PaymentStatuses = Field(description="Current payment status")
    amount: Decimal = Field(description="Payment amount")
    currency: str = Field(description="ISO 4217")
    created_at: datetime = Field(description="Creation moment in UTC")
    metadata: dict[str, Any] | None = Field(default=None, description="Arbitrary merchant data")
    failure_reason: FailureReasons | None = Field(default=None, description="Failure reason")
    error_message: str | None = Field(default=None, description="Error message")
    refunded_amount: Decimal = Field(
        default=Decimal("0"),
        description="Amount reserved or refunded across this payment's refunds",
    )

    def mark_pending(self) -> None:
        if PaymentStatuses.PENDING not in _ALLOWED_TRANSITIONS[self.status]:
            raise InvalidPaymentStatusTransitionError(
                from_status=self.status, to_status=PaymentStatuses.PENDING
            )
        self.status = PaymentStatuses.PENDING

    def mark_processing(self) -> None:
        if PaymentStatuses.PROCESSING not in _ALLOWED_TRANSITIONS[self.status]:
            raise InvalidPaymentStatusTransitionError(
                from_status=self.status, to_status=PaymentStatuses.PROCESSING
            )
        self.status = PaymentStatuses.PROCESSING

    def mark_failed(self, reason: FailureReasons) -> None:
        if PaymentStatuses.FAILED not in _ALLOWED_TRANSITIONS[self.status]:
            raise InvalidPaymentStatusTransitionError(
                from_status=self.status, to_status=PaymentStatuses.FAILED
            )
        if reason not in _ALLOWED_FAILURE_REASONS[self.status]:
            raise InvalidPaymentFailureReasonError(reason=reason, status=self.status)
        self.status = PaymentStatuses.FAILED
        self.failure_reason = reason

    def mark_error(self, error_message: str) -> None:
        if PaymentStatuses.ERROR not in _ALLOWED_TRANSITIONS[self.status]:
            raise InvalidPaymentStatusTransitionError(
                from_status=self.status, to_status=PaymentStatuses.ERROR
            )
        self.status = PaymentStatuses.ERROR
        self.error_message = error_message

    def mark_success(self) -> None:
        if PaymentStatuses.SUCCESS not in _ALLOWED_TRANSITIONS[self.status]:
            raise InvalidPaymentStatusTransitionError(
                from_status=self.status, to_status=PaymentStatuses.SUCCESS
            )
        self.status = PaymentStatuses.SUCCESS


_ALLOWED_TRANSITIONS: dict[PaymentStatuses, frozenset[PaymentStatuses]] = {
    PaymentStatuses.CREATED: frozenset({PaymentStatuses.PENDING}),
    PaymentStatuses.PENDING: frozenset({PaymentStatuses.PROCESSING, PaymentStatuses.FAILED}),
    PaymentStatuses.PROCESSING: frozenset(
        {PaymentStatuses.SUCCESS, PaymentStatuses.ERROR, PaymentStatuses.FAILED}
    ),
    PaymentStatuses.SUCCESS: frozenset(),
    PaymentStatuses.ERROR: frozenset(),
    PaymentStatuses.FAILED: frozenset(),
}

_ALLOWED_FAILURE_REASONS: dict[PaymentStatuses, frozenset[FailureReasons]] = {
    PaymentStatuses.PENDING: frozenset({FailureReasons.TIMEOUT}),
    PaymentStatuses.PROCESSING: frozenset(
        {FailureReasons.INSUFFICIENT_FUNDS, FailureReasons.FRAUD, FailureReasons.LIMIT_EXCEEDED}
    ),
}
