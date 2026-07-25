from abc import ABC, abstractmethod

from src.contexts.core_payment.domain.payment import Payment
from src.contexts.core_payment.domain.refund import Refund

# The only known provider; an "id -> provider" registry will appear
# together with the second provider.
FAKE_PROVIDER_ID = 1


class ProviderInitiationError(Exception):
    """Initiation did not succeed, and the outcome at the provider is unknown.

    The request may have been lost on the way there, or the answer lost on the
    way back — so the caller must not assume that nothing happened.
    """


class ProviderRejectedError(ProviderInitiationError):
    """The provider answered and refused: the operation does not exist there.

    The only case where the outcome is known, which is what makes it safe to
    close the refund and give the reserved amount back. A subclass so that
    callers who only care about "initiation failed" keep working unchanged.
    """


class PaymentProvider(ABC):
    @abstractmethod
    async def initiate_payment(self, payment: Payment) -> None: ...

    @abstractmethod
    async def initiate_refund(self, refund: Refund) -> None:
        """Implementations MUST deduplicate by refund.id.

        Reconciliation re-initiates refunds stuck in CREATED, and without
        deduplication on the provider side a repeat would send the payer their
        money a second time. For a real PSP that means passing refund.id as the
        provider's own idempotency key.
        """
