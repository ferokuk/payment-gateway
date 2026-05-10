from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from pydantic.v1 import Field

from src.shared.ioc import ConfigProvider


class CreatePaymentRequest(BaseModel):
    amount: Decimal = Field(gt=0, decimal_places=2)
    currency: str = Field(min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")
    provider_id: int = Field(gt=0)
    metadata: dict[str, Any] | None = None


class PaymentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    payment_id: UUID
    amount: Decimal
    status: str
    currency: str
    created_at: datetime
