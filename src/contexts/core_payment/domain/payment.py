from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from src.contexts.core_payment.domain.exceptions import InvalidPaymentStatusTransitionError


class PaymentStatuses(StrEnum):
    CREATED = "created"
    PENDING = "pending"
    PROCESSING = "processing"
    SUCCESS = "success"
    ERROR = "error"
    FAILED = "failed"


class FailureReasons(StrEnum):
    TIMEOUT = "timeout"
    INSUFFICIENT_FUNDS = "insufficient_funds"
    FRAUD = "fraud"
    LIMIT_EXCEEDED = "limit_exceeded"


class Payment(BaseModel):
    id: UUID = Field(description="ID созданного платежа")
    provider_id: int = Field(gt=0, description="ID провайдера")
    status: PaymentStatuses = Field(description="Текущий статус платежа")
    amount: Decimal = Field(description="Сумма платежа")
    currency: str = Field(description="ISO 4217")
    created_at: datetime = Field(description="Момент создания в UTC")
    metadata: dict[str, Any] | None = Field(
        default=None, description="Произвольные данные мерчанта"
    )
    failure_reason: FailureReasons | None = Field(default=None, description="Причина ошибки")
    error_message: str | None = Field(default=None, description="Текст ошибки")

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

    def mark_failed(self) -> None:
        if PaymentStatuses.FAILED not in _ALLOWED_TRANSITIONS[self.status]:
            raise InvalidPaymentStatusTransitionError(
                from_status=self.status, to_status=PaymentStatuses.FAILED
            )
        self.status = PaymentStatuses.FAILED

    def mark_error(self) -> None:
        if PaymentStatuses.ERROR not in _ALLOWED_TRANSITIONS[self.status]:
            raise InvalidPaymentStatusTransitionError(
                from_status=self.status, to_status=PaymentStatuses.ERROR
            )
        self.status = PaymentStatuses.ERROR

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
