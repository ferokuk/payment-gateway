from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

LEGACY_MERCHANT_ID = UUID("00000000-0000-4000-8000-000000000001")


@dataclass(frozen=True, slots=True)
class Merchant:
    id: UUID
    name: str
    is_active: bool
    created_at: datetime
