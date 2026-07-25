from uuid import UUID

import pytest
from pydantic import ValidationError
from src.contexts.core_payment.presentation.schemas.callback import RefundCallbackRequest

_REFUND_ID = UUID("0197b7a0-0000-7000-8000-000000000002")


def _payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {"refund_id": str(_REFUND_ID), "status": "success"}
    payload.update(overrides)
    return payload


@pytest.mark.parametrize(
    "payload",
    [
        _payload(status="success"),
        _payload(status="failed", failure_reason="timeout"),
        _payload(status="failed", failure_reason="card_unavailable"),
        _payload(status="failed", failure_reason="insufficient_merchant_balance"),
        _payload(status="error", error_message="Internal provider error"),
    ],
)
def test_accepts_valid_payloads(payload: dict[str, object]) -> None:
    request = RefundCallbackRequest.model_validate(payload)
    assert request.refund_id == _REFUND_ID


@pytest.mark.parametrize(
    "payload",
    [
        _payload(status="failed"),  # failed without failure_reason
        _payload(status="error"),  # error without error_message
        _payload(status="success", failure_reason="timeout"),
        _payload(status="success", error_message="boom"),
        _payload(status="error", error_message="boom", failure_reason="timeout"),
        _payload(status="pending"),  # not a refund callback status
        _payload(status="processing"),  # payment-only status
        _payload(status="failed", failure_reason="fraud"),  # payment-only reason
    ],
)
def test_rejects_invalid_payloads(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        RefundCallbackRequest.model_validate(payload)
