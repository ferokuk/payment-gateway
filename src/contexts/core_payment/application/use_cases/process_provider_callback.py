from typing import assert_never

import structlog

from src.contexts.core_payment.application.dto.callback import ProviderCallbackInputDTO
from src.contexts.core_payment.application.dto.payment import GetPaymentStatusOutputDTO
from src.contexts.core_payment.domain.exceptions import (
    InvalidPaymentStatusTransitionError,
    PaymentNotFoundError,
    StalePaymentStateError,
)
from src.contexts.core_payment.domain.statuses import PaymentStatuses
from src.contexts.core_payment.infrastructure.database.repositories import (
    SQLAlchemyPaymentRepository,
)

logger = structlog.get_logger(__name__)


class ProcessProviderCallbackUseCase:
    """Applies a provider callback to the payment.

    Duplicate idempotency is guaranteed by the state machine: payment already
    in the target status -> no-op, invalid transition -> domain error.
    """

    def __init__(self, payment_repository: SQLAlchemyPaymentRepository) -> None:
        self._payment_repository = payment_repository

    async def __call__(self, command: ProviderCallbackInputDTO) -> GetPaymentStatusOutputDTO:
        payment = await self._payment_repository.get_by_id(command.payment_id)
        if payment is None:
            logger.info("callback_payment_not_found", payment_id=str(command.payment_id))
            raise PaymentNotFoundError

        target = PaymentStatuses(command.status)
        from_status = payment.status  # capture BEFORE mark_* — needed on a CAS conflict

        if payment.status is target:
            logger.info(
                "callback_duplicate_ignored",
                payment_id=str(payment.id),
                status=command.status,
            )
            return GetPaymentStatusOutputDTO(
                payment_id=payment.id,
                status=payment.status,
                refunded_amount=payment.refunded_amount,
            )

        if command.status == "processing":
            payment.mark_processing()
        elif command.status == "success":
            payment.mark_success()
        elif command.status == "failed":
            assert command.failure_reason is not None  # guaranteed by the schema validator
            payment.mark_failed(command.failure_reason)
        elif command.status == "error":
            assert command.error_message is not None  # guaranteed by the schema validator
            payment.mark_error(command.error_message)
        else:
            assert_never(command.status)

        try:
            await self._payment_repository.update(payment, expected_status=from_status)
        except StalePaymentStateError as stale:
            fresh = await self._payment_repository.get_by_id(command.payment_id)
            if fresh is None:
                raise PaymentNotFoundError from stale
            if fresh.status is target:
                # A concurrent duplicate of the same webhook is already applied — no-op.
                logger.info(
                    "callback_duplicate_race_noop",
                    payment_id=str(fresh.id),
                    status=command.status,
                )
                return GetPaymentStatusOutputDTO(
                    payment_id=fresh.id,
                    status=fresh.status,
                    refunded_amount=fresh.refunded_amount,
                )
            raise InvalidPaymentStatusTransitionError(
                from_status=fresh.status, to_status=target
            ) from stale
        logger.info("callback_applied", payment_id=str(payment.id), status=payment.status.value)
        return GetPaymentStatusOutputDTO(
            payment_id=payment.id,
            status=payment.status,
            refunded_amount=payment.refunded_amount,
        )
