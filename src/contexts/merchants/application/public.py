from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class MerchantIdentity:
    """Trusted authentication result; the only identity exported to other contexts."""

    merchant_id: UUID
    api_key_id: UUID
