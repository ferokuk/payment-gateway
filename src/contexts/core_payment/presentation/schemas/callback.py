from uuid import UUID

from pydantic import BaseModel, Field, model_validator
from src.contexts.core_payment.application.dto.callback import CallbackStatus
from src.contexts.core_payment.application.dto.refund import RefundCallbackStatus
from src.contexts.core_payment.domain.statuses import FailureReasons, RefundFailureReasons


class ProviderCallbackRequest(BaseModel):
    payment_id: UUID = Field(description="Payment ID in our system")
    status: CallbackStatus = Field(description="New status from the provider")
    failure_reason: FailureReasons | None = Field(
        default=None, description="Failure reason (only for failed)"
    )
    error_message: str | None = Field(default=None, description="Error message (only for error)")

    @model_validator(mode="after")
    def _validate_shape(self) -> ProviderCallbackRequest:
        if self.status == "failed" and self.failure_reason is None:
            raise ValueError("failure_reason is required when status is 'failed'")
        if self.status != "failed" and self.failure_reason is not None:
            raise ValueError("failure_reason is only allowed when status is 'failed'")
        if self.status == "error" and self.error_message is None:
            raise ValueError("error_message is required when status is 'error'")
        if self.status != "error" and self.error_message is not None:
            raise ValueError("error_message is only allowed when status is 'error'")
        return self


class RefundCallbackRequest(BaseModel):
    refund_id: UUID = Field(description="Refund ID in our system")
    status: RefundCallbackStatus = Field(description="New status from the provider")
    failure_reason: RefundFailureReasons | None = Field(
        default=None, description="Failure reason (only for failed)"
    )
    error_message: str | None = Field(default=None, description="Error message (only for error)")

    @model_validator(mode="after")
    def _validate_shape(self) -> RefundCallbackRequest:
        if self.status == "failed" and self.failure_reason is None:
            raise ValueError("failure_reason is required when status is 'failed'")
        if self.status != "failed" and self.failure_reason is not None:
            raise ValueError("failure_reason is only allowed when status is 'failed'")
        if self.status == "error" and self.error_message is None:
            raise ValueError("error_message is required when status is 'error'")
        if self.status != "error" and self.error_message is not None:
            raise ValueError("error_message is only allowed when status is 'error'")
        return self
