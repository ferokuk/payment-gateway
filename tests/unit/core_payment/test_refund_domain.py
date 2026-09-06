from datetime import UTC, datetime
from decimal import Decimal

import pytest
from src.contexts.core_payment.domain.exceptions import (
    InvalidRefundFailureReasonError,
    InvalidRefundStatusTransitionError,
)
from src.contexts.core_payment.domain.refund import (
    _ALLOWED_FAILURE_REASONS,
    _ALLOWED_TRANSITIONS,
    Refund,
)
from src.contexts.core_payment.domain.statuses import RefundFailureReasons, RefundStatuses
from src.shared.ids import new_uuid
from tests.fixtures.merchants import MERCHANT_ID
from tests.fixtures.refund import make_refund


def test_refund_is_created_with_given_fields() -> None:
    payment_id = new_uuid()
    refund = Refund(
        merchant_id=MERCHANT_ID,
        id=new_uuid(),
        payment_id=payment_id,
        amount=Decimal("40.00"),
        status=RefundStatuses.CREATED,
        created_at=datetime.now(UTC),
    )

    assert refund.status is RefundStatuses.CREATED
    assert refund.payment_id == payment_id
    assert refund.amount == Decimal("40.00")
    assert refund.metadata is None
    assert refund.failure_reason is None
    assert refund.error_message is None


# --- Status transitions (happy path) ---


def test_mark_pending_from_created() -> None:
    refund = make_refund(status=RefundStatuses.CREATED)

    refund.mark_pending()

    assert refund.status is RefundStatuses.PENDING


def test_mark_success_from_pending() -> None:
    refund = make_refund(status=RefundStatuses.PENDING)

    refund.mark_success()

    assert refund.status is RefundStatuses.SUCCESS


# --- Status transitions (forbidden) ---


@pytest.mark.parametrize(
    ("initial", "method"),
    [
        (RefundStatuses.CREATED, "mark_success"),
        (RefundStatuses.PENDING, "mark_pending"),
        (RefundStatuses.SUCCESS, "mark_pending"),
        (RefundStatuses.SUCCESS, "mark_success"),
        (RefundStatuses.FAILED, "mark_pending"),
        (RefundStatuses.FAILED, "mark_success"),
        (RefundStatuses.ERROR, "mark_pending"),
    ],
)
def test_invalid_transition_raises(initial: RefundStatuses, method: str) -> None:
    refund = make_refund(status=initial)

    with pytest.raises(InvalidRefundStatusTransitionError):
        getattr(refund, method)()

    assert refund.status is initial


def test_invalid_transition_error_carries_context() -> None:
    refund = make_refund(status=RefundStatuses.CREATED)

    with pytest.raises(InvalidRefundStatusTransitionError) as exc_info:
        refund.mark_success()

    assert exc_info.value.from_status is RefundStatuses.CREATED
    assert exc_info.value.to_status is RefundStatuses.SUCCESS


# --- mark_failed ---


@pytest.mark.parametrize(
    "reason",
    [
        RefundFailureReasons.TIMEOUT,
        RefundFailureReasons.CARD_UNAVAILABLE,
        RefundFailureReasons.INSUFFICIENT_MERCHANT_BALANCE,
    ],
)
def test_mark_failed_from_pending_sets_status_and_reason(reason: RefundFailureReasons) -> None:
    refund = make_refund(status=RefundStatuses.PENDING)

    refund.mark_failed(reason)

    assert refund.status is RefundStatuses.FAILED
    assert refund.failure_reason is reason


@pytest.mark.parametrize(
    "initial",
    [
        RefundStatuses.SUCCESS,
        RefundStatuses.FAILED,
    ],
)
def test_mark_failed_from_non_failable_state_raises_transition_error(
    initial: RefundStatuses,
) -> None:
    refund = make_refund(status=initial)

    with pytest.raises(InvalidRefundStatusTransitionError):
        refund.mark_failed(RefundFailureReasons.TIMEOUT)

    assert refund.status is initial
    assert refund.failure_reason is None


def test_mark_failed_from_created_when_provider_did_not_accept() -> None:
    # The exit for a refund the provider never took: reconciliation closes it
    # and gives the reserved amount back.
    refund = make_refund(status=RefundStatuses.CREATED)

    refund.mark_failed(RefundFailureReasons.NOT_ACCEPTED_BY_PROVIDER)

    assert refund.status is RefundStatuses.FAILED
    assert refund.failure_reason is RefundFailureReasons.NOT_ACCEPTED_BY_PROVIDER


@pytest.mark.parametrize(
    "reason",
    [
        RefundFailureReasons.TIMEOUT,
        RefundFailureReasons.CARD_UNAVAILABLE,
        RefundFailureReasons.INSUFFICIENT_MERCHANT_BALANCE,
    ],
)
def test_mark_failed_from_created_rejects_verdict_reasons(reason: RefundFailureReasons) -> None:
    # These reasons are the provider's verdict on a refund it was working on;
    # a refund it never accepted cannot have one.
    refund = make_refund(status=RefundStatuses.CREATED)

    with pytest.raises(InvalidRefundFailureReasonError):
        refund.mark_failed(reason)

    assert refund.status is RefundStatuses.CREATED
    assert refund.failure_reason is None


def test_mark_failed_from_pending_rejects_not_accepted_reason() -> None:
    # Mirror guard: the refund is already accepted, so "not accepted" is a lie.
    refund = make_refund(status=RefundStatuses.PENDING)

    with pytest.raises(InvalidRefundFailureReasonError):
        refund.mark_failed(RefundFailureReasons.NOT_ACCEPTED_BY_PROVIDER)

    assert refund.status is RefundStatuses.PENDING


# --- mark_error ---


def test_mark_error_sets_status_and_message() -> None:
    refund = make_refund(status=RefundStatuses.PENDING)

    refund.mark_error("provider exploded")

    assert refund.status is RefundStatuses.ERROR
    assert refund.error_message == "provider exploded"


@pytest.mark.parametrize(
    "initial",
    [
        RefundStatuses.CREATED,
        RefundStatuses.SUCCESS,
        RefundStatuses.FAILED,
        RefundStatuses.ERROR,
    ],
)
def test_mark_error_from_invalid_state_raises(initial: RefundStatuses) -> None:
    refund = make_refund(status=initial)

    with pytest.raises(InvalidRefundStatusTransitionError):
        refund.mark_error("boom")


# --- Leaving error: the outcome became known ---


def test_mark_success_from_error() -> None:
    # ERROR means "outcome unknown", not "the end": once the provider finally
    # tells us what happened, the refund must be able to reach the real status.
    refund = make_refund(status=RefundStatuses.ERROR)

    refund.mark_success()

    assert refund.status is RefundStatuses.SUCCESS


@pytest.mark.parametrize(
    "reason",
    [
        RefundFailureReasons.TIMEOUT,
        RefundFailureReasons.CARD_UNAVAILABLE,
        RefundFailureReasons.INSUFFICIENT_MERCHANT_BALANCE,
    ],
)
def test_mark_failed_from_error(reason: RefundFailureReasons) -> None:
    refund = make_refund(status=RefundStatuses.ERROR)

    refund.mark_failed(reason)

    assert refund.status is RefundStatuses.FAILED
    assert refund.failure_reason is reason


def test_mark_failed_from_error_rejects_not_accepted_reason() -> None:
    # To reach error the refund passed through pending, so the provider did
    # take it — claiming it was never accepted contradicts the history.
    refund = make_refund(status=RefundStatuses.ERROR)

    with pytest.raises(InvalidRefundFailureReasonError):
        refund.mark_failed(RefundFailureReasons.NOT_ACCEPTED_BY_PROVIDER)

    assert refund.status is RefundStatuses.ERROR


# --- Invariant: reasons are defined for all failable statuses ---


def test_failure_reason_map_matches_failable_states() -> None:
    failable = {
        status
        for status, targets in _ALLOWED_TRANSITIONS.items()
        if RefundStatuses.FAILED in targets
    }

    # Every status that can transition to FAILED must have a set of reasons,
    # otherwise mark_failed raises KeyError when accessing _ALLOWED_FAILURE_REASONS.
    assert set(_ALLOWED_FAILURE_REASONS) == failable
    assert all(reasons for reasons in _ALLOWED_FAILURE_REASONS.values())
