from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from src.contexts.core_payment.domain.statuses import FailureReasons

# Statuses a provider may send in a callback. A subset of
# PaymentStatuses: created/pending cannot arrive from outside.
CallbackStatus = Literal["processing", "success", "failed", "error"]


class ProviderCallbackInputDTO(BaseModel):
    payment_id: UUID = Field(description="Payment ID in our system")
    status: CallbackStatus = Field(description="New status from the provider")
    failure_reason: FailureReasons | None = Field(
        default=None, description="Failure reason (only for failed)"
    )
    error_message: str | None = Field(default=None, description="Error message (only for error)")
