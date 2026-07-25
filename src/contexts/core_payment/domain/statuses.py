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


class RefundStatuses(StrEnum):
    CREATED = "created"
    PENDING = "pending"
    SUCCESS = "success"
    FAILED = "failed"
    ERROR = "error"


class RefundFailureReasons(StrEnum):
    TIMEOUT = "timeout"
    # The card was closed or expired on the payer side.
    CARD_UNAVAILABLE = "card_unavailable"
    INSUFFICIENT_MERCHANT_BALANCE = "insufficient_merchant_balance"
    # The provider refused to take the refund at all, so it never existed on
    # their side. Unlike the reasons above this is not a verdict on a refund in
    # progress — it is the only reason that can close a refund still in CREATED.
    NOT_ACCEPTED_BY_PROVIDER = "not_accepted_by_provider"
