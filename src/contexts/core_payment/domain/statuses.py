from enum import StrEnum


class PaymentStatuses(StrEnum):
    CREATED = "created"
    PENDING = "pending"
    PROCESSING = "processing"
    SUCCESS = "success"
    ERROR = "error"
    FAILED = "failed"


class FailureReasons(StrEnum):
    TIMEOUT = "timeout"
    INSUFFICIENT_FUNDS = "insufficient_funds"
    FRAUD = "fraud"
    LIMIT_EXCEEDED = "limit_exceeded"
