from typing import assert_never

import structlog

from src.contexts.core_payment.application.dto.refund import (
    GetRefundStatusOutputDTO,
    RefundCallbackInputDTO,
)
from src.contexts.core_payment.domain.exceptions import (
    InvalidRefundStatusTransitionError,
    RefundNotFoundError,
    StaleRefundStateError,
)
from src.contexts.core_payment.domain.refund import Refund
from src.contexts.core_payment.domain.statuses import RefundStatuses
from src.contexts.core_payment.infrastructure.database.repositories import (
    SQLAlchemyPaymentRepository,
    SQLAlchemyRefundRepository,
)

logger = structlog.get_logger(__name__)


class ProcessRefundCallbackUseCase:
    """Applies a provider callback to the refund.

    Mirrors the payment callback use case, plus reservation bookkeeping:
    FAILED releases the reserved amount in the same request transaction as the
    status CAS; ERROR holds it — the outcome at the provider is unknown, and
    releasing before manual reconciliation could allow over-refunding.
    """

    def __init__(
        self,
        refund_repository: SQLAlchemyRefundRepository,
        payment_repository: SQLAlchemyPaymentRepository,
    ) -> None:
        self._refund_repository = refund_repository
        self._payment_repository = payment_repository

    async def __call__(self, command: RefundCallbackInputDTO) -> GetRefundStatusOutputDTO:
        refund = await self._refund_repository.get_by_id(command.refund_id)
        if refund is None:
            logger.info("refund_callback_refund_not_found", refund_id=str(command.refund_id))
            raise RefundNotFoundError

        target = RefundStatuses(command.status)
        from_status = refund.status  # capture BEFORE mark_* — needed on a CAS conflict

        if refund.status is target:
            logger.info(
                "refund_callback_duplicate_ignored",
                refund_id=str(refund.id),
                status=command.status,
            )
            return _to_dto(refund)

        if command.status == "success":
            refund.mark_success()
        elif command.status == "failed":
            assert command.failure_reason is not None  # guaranteed by the schema validator
            refund.mark_failed(command.failure_reason)
        elif command.status == "error":
            assert command.error_message is not None  # guaranteed by the schema validator
            refund.mark_error(command.error_message)
        else:
            assert_never(command.status)

        try:
            await self._refund_repository.update(refund, expected_status=from_status)
        except StaleRefundStateError as stale:
            fresh = await self._refund_repository.get_by_id(command.refund_id)
            if fresh is None:
                raise RefundNotFoundError from stale
            if fresh.status is target:
                # A concurrent duplicate of the same webhook is already applied —
                # no-op; the winner did the release (if any), we must not repeat it.
                logger.info(
                    "refund_callback_duplicate_race_noop",
                    refund_id=str(fresh.id),
                    status=command.status,
                )
                return _to_dto(fresh)
            raise InvalidRefundStatusTransitionError(
                from_status=fresh.status, to_status=target
            ) from stale

        if refund.status is RefundStatuses.FAILED:
            # Release only after the CAS is won: exactly one of two duplicate
            # 'failed' callbacks reaches this line — no double release. Both
            # statements commit together at the DI root.
            await self._payment_repository.release_refund_amount(refund.payment_id, refund.amount)

        logger.info("refund_callback_applied", refund_id=str(refund.id), status=refund.status.value)
        return _to_dto(refund)


def _to_dto(refund: Refund) -> GetRefundStatusOutputDTO:
    return GetRefundStatusOutputDTO(
        refund_id=refund.id,
        payment_id=refund.payment_id,
        status=refund.status,
        amount=refund.amount,
    )
