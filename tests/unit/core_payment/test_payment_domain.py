from datetime import UTC, datetime
from decimal import Decimal

import pytest
from src.contexts.core_payment.domain.exceptions import (
    InvalidPaymentFailureReasonError,
    InvalidPaymentStatusTransitionError,
)
from src.contexts.core_payment.domain.payment import (
    _ALLOWED_FAILURE_REASONS,
    _ALLOWED_TRANSITIONS,
    Payment,
)
from src.contexts.core_payment.domain.statuses import FailureReasons, PaymentStatuses
from src.shared.ids import new_uuid
from tests.fixtures.payment import make_payment


def test_payment_is_created_with_given_fields() -> None:
    payment = Payment(
        id=new_uuid(),
        provider_id=1,
        status=PaymentStatuses.CREATED,
        amount=Decimal("100.00"),
        currency="USD",
        created_at=datetime.now(UTC),
    )

    assert payment.status is PaymentStatuses.CREATED
    assert payment.amount == Decimal("100.00")
    assert payment.metadata is None


# --- Переходы статусов (happy path) ---


@pytest.mark.parametrize(
    ("initial", "method", "expected"),
    [
        (PaymentStatuses.CREATED, "mark_pending", PaymentStatuses.PENDING),
        (PaymentStatuses.PENDING, "mark_processing", PaymentStatuses.PROCESSING),
        (PaymentStatuses.PROCESSING, "mark_success", PaymentStatuses.SUCCESS),
    ],
)
def test_valid_transition_changes_status(
    initial: PaymentStatuses, method: str, expected: PaymentStatuses
) -> None:
    payment = make_payment(status=initial)

    getattr(payment, method)()

    assert payment.status is expected


# --- Переходы статусов (запрещённые) ---


@pytest.mark.parametrize(
    ("initial", "method"),
    [
        (PaymentStatuses.CREATED, "mark_processing"),
        (PaymentStatuses.CREATED, "mark_success"),
        (PaymentStatuses.PENDING, "mark_pending"),
        (PaymentStatuses.PENDING, "mark_success"),
        (PaymentStatuses.PROCESSING, "mark_pending"),
        (PaymentStatuses.PROCESSING, "mark_processing"),
        (PaymentStatuses.SUCCESS, "mark_pending"),
        (PaymentStatuses.SUCCESS, "mark_success"),
        (PaymentStatuses.FAILED, "mark_processing"),
        (PaymentStatuses.ERROR, "mark_processing"),
    ],
)
def test_invalid_transition_raises(initial: PaymentStatuses, method: str) -> None:
    payment = make_payment(status=initial)

    with pytest.raises(InvalidPaymentStatusTransitionError):
        getattr(payment, method)()

    assert payment.status is initial


def test_invalid_transition_error_carries_context() -> None:
    payment = make_payment(status=PaymentStatuses.CREATED)

    with pytest.raises(InvalidPaymentStatusTransitionError) as exc_info:
        payment.mark_success()

    assert exc_info.value.from_status is PaymentStatuses.CREATED
    assert exc_info.value.to_status is PaymentStatuses.SUCCESS


# --- mark_failed: разрешённые пары (статус, причина) ---


@pytest.mark.parametrize(
    ("initial", "reason"),
    [
        (PaymentStatuses.PENDING, FailureReasons.TIMEOUT),
        (PaymentStatuses.PROCESSING, FailureReasons.INSUFFICIENT_FUNDS),
        (PaymentStatuses.PROCESSING, FailureReasons.FRAUD),
        (PaymentStatuses.PROCESSING, FailureReasons.LIMIT_EXCEEDED),
    ],
)
def test_mark_failed_sets_status_and_reason(
    initial: PaymentStatuses, reason: FailureReasons
) -> None:
    payment = make_payment(status=initial)

    payment.mark_failed(reason)

    assert payment.status is PaymentStatuses.FAILED
    assert payment.failure_reason is reason


# --- mark_failed: переход разрешён, но причина не подходит источнику ---


@pytest.mark.parametrize(
    ("initial", "reason"),
    [
        (PaymentStatuses.PENDING, FailureReasons.FRAUD),
        (PaymentStatuses.PENDING, FailureReasons.INSUFFICIENT_FUNDS),
        (PaymentStatuses.PENDING, FailureReasons.LIMIT_EXCEEDED),
        (PaymentStatuses.PROCESSING, FailureReasons.TIMEOUT),
    ],
)
def test_mark_failed_with_disallowed_reason_raises(
    initial: PaymentStatuses, reason: FailureReasons
) -> None:
    payment = make_payment(status=initial)

    with pytest.raises(InvalidPaymentFailureReasonError):
        payment.mark_failed(reason)

    assert payment.status is initial
    assert payment.failure_reason is None


def test_invalid_failure_reason_error_carries_context() -> None:
    payment = make_payment(status=PaymentStatuses.PENDING)

    with pytest.raises(InvalidPaymentFailureReasonError) as exc_info:
        payment.mark_failed(FailureReasons.FRAUD)

    assert exc_info.value.status is PaymentStatuses.PENDING
    assert exc_info.value.reason is FailureReasons.FRAUD


# --- mark_failed: из нефейлящихся статусов сначала падает проверка перехода ---


@pytest.mark.parametrize(
    "initial",
    [
        PaymentStatuses.CREATED,
        PaymentStatuses.SUCCESS,
        PaymentStatuses.ERROR,
        PaymentStatuses.FAILED,
    ],
)
def test_mark_failed_from_non_failable_state_raises_transition_error(
    initial: PaymentStatuses,
) -> None:
    payment = make_payment(status=initial)

    with pytest.raises(InvalidPaymentStatusTransitionError):
        payment.mark_failed(FailureReasons.TIMEOUT)


# --- mark_error ---


def test_mark_error_sets_status_and_message() -> None:
    payment = make_payment(status=PaymentStatuses.PROCESSING)

    payment.mark_error("gateway timeout")

    assert payment.status is PaymentStatuses.ERROR
    assert payment.error_message == "gateway timeout"


@pytest.mark.parametrize(
    "initial",
    [
        PaymentStatuses.CREATED,
        PaymentStatuses.PENDING,
        PaymentStatuses.SUCCESS,
        PaymentStatuses.ERROR,
        PaymentStatuses.FAILED,
    ],
)
def test_mark_error_from_invalid_state_raises(initial: PaymentStatuses) -> None:
    payment = make_payment(status=initial)

    with pytest.raises(InvalidPaymentStatusTransitionError):
        payment.mark_error("boom")


# --- Инвариант: причины описаны для всех failable-статусов ---


def test_failure_reason_map_matches_failable_states() -> None:
    failable = {
        status
        for status, targets in _ALLOWED_TRANSITIONS.items()
        if PaymentStatuses.FAILED in targets
    }

    # Каждый статус, из которого можно уйти в FAILED, обязан иметь набор причин,
    # иначе mark_failed поднимет KeyError при обращении к _ALLOWED_FAILURE_REASONS.
    assert set(_ALLOWED_FAILURE_REASONS) == failable
    assert all(reasons for reasons in _ALLOWED_FAILURE_REASONS.values())
