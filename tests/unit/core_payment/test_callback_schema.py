from uuid import UUID

import pytest
from pydantic import ValidationError
from src.contexts.core_payment.presentation.schemas.callback import ProviderCallbackRequest

_PAYMENT_ID = UUID("0197b7a0-0000-7000-8000-000000000001")


def _payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {"payment_id": str(_PAYMENT_ID), "status": "processing"}
    payload.update(overrides)
    return payload


@pytest.mark.parametrize(
    "payload",
    [
        _payload(status="processing"),
        _payload(status="success"),
        _payload(status="failed", failure_reason="insufficient_funds"),
        _payload(status="failed", failure_reason="timeout"),
        _payload(status="error", error_message="Internal provider error"),
    ],
)
def test_accepts_valid_payloads(payload: dict[str, object]) -> None:
    request = ProviderCallbackRequest.model_validate(payload)
    assert request.payment_id == _PAYMENT_ID


@pytest.mark.parametrize(
    "payload",
    [
        _payload(status="failed"),  # failed without failure_reason
        _payload(status="error"),  # error without error_message
        _payload(status="processing", failure_reason="fraud"),
        _payload(status="success", failure_reason="fraud"),
        _payload(status="processing", error_message="boom"),
        _payload(status="success", error_message="boom"),
        _payload(status="failed", failure_reason="fraud", error_message="boom"),
        _payload(status="error", error_message="boom", failure_reason="fraud"),
        _payload(status="pending"),  # not a callback status
        _payload(status="failed", failure_reason="unknown_reason"),
    ],
)
def test_rejects_invalid_payloads(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ProviderCallbackRequest.model_validate(payload)
