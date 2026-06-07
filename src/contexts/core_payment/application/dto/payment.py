from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import UUID7, BaseModel, Field

from src.contexts.core_payment.domain.statuses import PaymentStatuses


class CreatePaymentInputDTO(BaseModel):
    amount: Decimal = Field(gt=0, decimal_places=2, description="Сумма платежа")
    currency: str = Field(min_length=3, max_length=3, pattern=r"^[A-Z]{3}$", description="ISO 4217")
    provider_id: int = Field(gt=0, description="ID платёжного провайдера")
    metadata: dict[str, Any] | None = Field(
        default=None, description="Произвольные данные мерчанта"
    )


class CreatePaymentOutputDTO(BaseModel):
    payment_id: UUID7 = Field(description="ID созданного платежа")
    status: PaymentStatuses = Field(description="Текущий статус платежа")
    amount: Decimal = Field(description="Сумма платежа")
    currency: str = Field(description="ISO 4217")
    created_at: datetime = Field(description="Момент создания в UTC")


class GetPaymentStatusOutputDTO(BaseModel):
    payment_id: UUID7 = Field(description="ID созданного платежа")
    status: PaymentStatuses = Field(description="Текущий статус платежа")
