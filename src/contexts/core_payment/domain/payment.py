from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID


class PaymentStatus(Enum):
    CREATED = "created"
    PENDING = "pending"
    PROCESSING = "processing"
    SUCCESS = "success"
    ERROR = "error"
    FAILED = "failed"


@dataclass
class Payment:
    id: UUID
    amount: Decimal
    currency: str
    provider_id: int
    status: PaymentStatus
    metadata: dict[str, Any] | None
    created_at: datetime
