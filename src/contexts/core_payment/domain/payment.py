from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import UUID7, BaseModel, Field


class PaymentStatuses(StrEnum):
    CREATED = "created"
    PENDING = "pending"
    PROCESSING = "processing"
    SUCCESS = "success"
    ERROR = "error"
    FAILED = "failed"


class Payment(BaseModel):
    id: UUID7 = Field(description="ID созданного платежа")
    provider_id: int = Field(gt=0)
    status: PaymentStatuses = Field(description="Текущий статус платежа")
    amount: Decimal = Field(description="Сумма платежа")
    currency: str = Field(description="ISO 4217")
    created_at: datetime = Field(description="Момент создания в UTC")
    metadata: dict[str, Any] | None = Field(
        default=None, description="Произвольные данные мерчанта"
    )
