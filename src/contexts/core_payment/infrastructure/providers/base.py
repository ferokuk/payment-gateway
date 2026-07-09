from abc import ABC, abstractmethod

from src.contexts.core_payment.domain.payment import Payment

# The only known provider; an "id -> provider" registry will appear
# together with the second provider.
FAKE_PROVIDER_ID = 1


class ProviderInitiationError(Exception):
    """The provider is unavailable or rejected the payment initiation."""


class PaymentProvider(ABC):
    @abstractmethod
    async def initiate_payment(self, payment: Payment) -> None: ...
