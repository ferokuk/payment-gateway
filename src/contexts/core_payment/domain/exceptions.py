from uuid import UUID

from src.contexts.core_payment.domain.statuses import FailureReasons, PaymentStatuses


class PaymentNotFoundError(Exception):
    pass


class StalePaymentStateError(Exception):
    """Payment changed concurrently: the status is no longer the one the transition started from."""

    payment_id: UUID
    expected_status: PaymentStatuses

    def __init__(self, payment_id: UUID, expected_status: PaymentStatuses) -> None:
        self.payment_id = payment_id
        self.expected_status = expected_status

    def __str__(self) -> str:
        return (
            f"Payment {self.payment_id} is no longer in {self.expected_status}: "
            "concurrent update detected"
        )


class InvalidPaymentStatusTransitionError(Exception):
    from_status: PaymentStatuses
    to_status: PaymentStatuses

    def __init__(self, from_status: PaymentStatuses, to_status: PaymentStatuses):
        self.from_status = from_status
        self.to_status = to_status

    def __str__(self) -> str:
        return f"Cannot transition from {self.from_status} to {self.to_status}"


class InvalidPaymentFailureReasonError(Exception):
    status: PaymentStatuses
    reason: FailureReasons

    def __init__(self, status: PaymentStatuses, reason: FailureReasons):
        self.status = status
        self.reason = reason

    def __str__(self) -> str:
        return f"Reason {self.reason} is not allowed when failing from {self.status}"


class UnknownProviderError(Exception):
    provider_id: int

    def __init__(self, provider_id: int) -> None:
        self.provider_id = provider_id

    def __str__(self) -> str:
        return f"Unknown provider_id={self.provider_id}"


class IdempotencyKeyMismatchError(Exception):
    key: str

    def __init__(self, key: str) -> None:
        self.key = key

    def __str__(self) -> str:
        return f"Idempotency key {self.key!r} was already used with a different request body"
