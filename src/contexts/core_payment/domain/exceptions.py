from src.contexts.core_payment.domain.statuses import FailureReasons, PaymentStatuses


class PaymentNotFoundError(Exception):
    pass


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
