from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, cast
from uuid import UUID

from sqlalchemy import CursorResult, func, select
from sqlalchemy import update as sql_update
from sqlalchemy.ext.asyncio import AsyncSession

from src.contexts.core_payment.domain.exceptions import (
    PaymentNotFoundError,
    PaymentNotRefundableError,
    RefundAmountExceededError,
    RefundNotFoundError,
    StalePaymentStateError,
    StaleRefundStateError,
)
from src.contexts.core_payment.domain.payment import Payment
from src.contexts.core_payment.domain.refund import Refund
from src.contexts.core_payment.domain.statuses import PaymentStatuses, RefundStatuses
from src.contexts.core_payment.infrastructure.database.models import (
    IdempotencyKeyModel,
    PaymentModel,
    RefundIdempotencyKeyModel,
    RefundModel,
)


@dataclass(frozen=True)
class IdempotencyRecord:
    key: str
    request_hash: str
    payment_id: UUID
    response_body: dict[str, Any] | None = None


@dataclass(frozen=True)
class RefundIdempotencyRecord:
    key: str
    request_hash: str
    refund_id: UUID
    response_body: dict[str, Any] | None = None


class SQLAlchemyPaymentRepository:
    def __init__(self, session: AsyncSession):
        self._session = session

    async def add(self, payment: Payment) -> None:
        model = self._to_model(payment)
        self._session.add(model)

    async def get_by_id(self, payment_id: UUID) -> Payment | None:
        # populate_existing: a re-read after a CAS conflict must issue a fresh
        # SELECT rather than return the object from the identity map.
        model = await self._session.get(PaymentModel, payment_id, populate_existing=True)
        return self._to_domain(model) if model else None

    async def update(self, payment: Payment, *, expected_status: PaymentStatuses) -> None:
        # A payment transition is an atomic CAS: the guard on the from-status
        # protects against overwriting state that has moved ahead.
        stmt = (
            sql_update(PaymentModel)
            .where(PaymentModel.id == payment.id, PaymentModel.status == expected_status)
            .values(
                status=payment.status,
                failure_reason=payment.failure_reason,
                error_message=payment.error_message,
            )
        )
        result = await self._session.execute(stmt)
        # AsyncSession.execute is typed via overload as Result[Any];
        # for an UpdateBase statement the runtime type is CursorResult with
        # .rowcount, but that is not expressed statically, hence the cast.
        cursor_result = cast("CursorResult[Any]", result)
        if cursor_result.rowcount == 0:
            # Distinguish "payment does not exist" from "a competitor got there first".
            model = await self._session.get(PaymentModel, payment.id, populate_existing=True)
            if model is None:
                raise PaymentNotFoundError
            raise StalePaymentStateError(payment.id, expected_status)

    async def reserve_refund_amount(self, payment_id: UUID, amount: Decimal) -> None:
        # One atomic guarded UPDATE closes the check-then-act window: two
        # concurrent reservations cannot both pass the remainder check.
        stmt = (
            sql_update(PaymentModel)
            .where(
                PaymentModel.id == payment_id,
                PaymentModel.status == PaymentStatuses.SUCCESS,
                PaymentModel.refunded_amount + amount <= PaymentModel.amount,
            )
            .values(refunded_amount=PaymentModel.refunded_amount + amount)
        )
        result = await self._session.execute(stmt)
        cursor_result = cast("CursorResult[Any]", result)
        if cursor_result.rowcount == 0:
            # Distinguish missing payment / wrong status / exhausted remainder.
            model = await self._session.get(PaymentModel, payment_id, populate_existing=True)
            if model is None:
                raise PaymentNotFoundError
            if model.status is not PaymentStatuses.SUCCESS:
                raise PaymentNotRefundableError(payment_id, model.status)
            raise RefundAmountExceededError(payment_id, amount)

    async def release_refund_amount(self, payment_id: UUID, amount: Decimal) -> None:
        # The mirror guarded UPDATE: a release must never drive the counter
        # negative — that would mean a double release, i.e. an application bug.
        stmt = (
            sql_update(PaymentModel)
            .where(
                PaymentModel.id == payment_id,
                PaymentModel.refunded_amount - amount >= 0,
            )
            .values(refunded_amount=PaymentModel.refunded_amount - amount)
        )
        result = await self._session.execute(stmt)
        cursor_result = cast("CursorResult[Any]", result)
        if cursor_result.rowcount == 0:
            model = await self._session.get(PaymentModel, payment_id, populate_existing=True)
            if model is None:
                raise PaymentNotFoundError
            raise LookupError(
                f"Releasing {amount} for payment {payment_id} would make refunded_amount negative"
            )

    @staticmethod
    def _to_model(payment: Payment) -> PaymentModel:
        return PaymentModel(
            id=payment.id,
            provider_id=payment.provider_id,
            amount=payment.amount,
            currency=payment.currency,
            meta=payment.metadata,
            status=payment.status,
            created_at=payment.created_at,
            failure_reason=payment.failure_reason,
            error_message=payment.error_message,
            refunded_amount=payment.refunded_amount,
        )

    @staticmethod
    def _to_domain(payment: PaymentModel) -> Payment:
        return Payment(
            id=payment.id,
            provider_id=payment.provider_id,
            amount=payment.amount,
            currency=payment.currency,
            metadata=payment.meta,
            status=payment.status,
            created_at=payment.created_at,
            failure_reason=payment.failure_reason,
            error_message=payment.error_message,
            refunded_amount=payment.refunded_amount,
        )


class SQLAlchemyIdempotencyKeyRepository:
    def __init__(self, session: AsyncSession):
        self._session = session

    async def get(self, key: str) -> IdempotencyRecord | None:
        # populate_existing: re-reading the key needs a fresh SELECT rather
        # than an object cached in the identity map (the resume algorithm).
        model = await self._session.get(IdempotencyKeyModel, key, populate_existing=True)
        if model is None:
            return None
        return IdempotencyRecord(
            key=model.key,
            request_hash=model.request_hash,
            payment_id=model.payment_id,
            response_body=model.response_body,
        )

    async def add(self, record: IdempotencyRecord) -> None:
        self._session.add(
            IdempotencyKeyModel(
                key=record.key,
                request_hash=record.request_hash,
                payment_id=record.payment_id,
                response_body=record.response_body,
            )
        )
        # flush inside the SAVEPOINT: a key conflict must surface as an
        # IntegrityError here, where the use case catches it, not at the final commit.
        await self._session.flush()

    async def set_response(self, key: str, response_body: dict[str, Any]) -> None:
        model = await self._session.get(IdempotencyKeyModel, key)
        if model is None:  # the key is reserved before initiation — must not get here
            raise LookupError(f"Idempotency key {key!r} is not reserved")
        model.response_body = response_body


class SQLAlchemyRefundRepository:
    def __init__(self, session: AsyncSession):
        self._session = session

    async def add(self, refund: Refund) -> None:
        self._session.add(self._to_model(refund))

    async def get_by_id(self, refund_id: UUID) -> Refund | None:
        # populate_existing: a re-read after a CAS conflict must issue a fresh
        # SELECT rather than return the object from the identity map.
        model = await self._session.get(RefundModel, refund_id, populate_existing=True)
        return self._to_domain(model) if model else None

    async def list_by_payment_id(self, payment_id: UUID) -> list[Refund]:
        # Ordering belongs in SQL, not in Python: the API promises creation
        # order, and id breaks ties between refunds born in the same clock tick
        # (uuid7 is time-ordered, so the tiebreaker follows the same axis).
        stmt = (
            select(RefundModel)
            .where(RefundModel.payment_id == payment_id)
            .order_by(RefundModel.created_at, RefundModel.id)
        )
        models = (await self._session.execute(stmt)).scalars().all()
        return [self._to_domain(model) for model in models]

    async def list_stuck_created(
        self, *, created_before: datetime, created_after: datetime, limit: int
    ) -> list[Refund]:
        """Refunds that never left CREATED and can still be re-initiated safely.

        Bounded on both sides: too young means still in flight, too old means
        the provider has likely forgotten the deduplication key, and a repeat
        would create a second real refund.
        """
        # Deliberately without FOR UPDATE: the lock would be held across the
        # provider HTTP call and block a legitimate callback for that refund.
        # Safety rests on the CAS in update() plus deduplication on the provider
        # side — a second reconciler wastes a call but cannot refund twice.
        stmt = (
            select(RefundModel)
            .where(
                RefundModel.status == RefundStatuses.CREATED,
                RefundModel.created_at < created_before,
                RefundModel.created_at >= created_after,
            )
            .order_by(RefundModel.created_at)
            .limit(limit)
        )
        models = (await self._session.execute(stmt)).scalars().all()
        return [self._to_domain(model) for model in models]

    async def count_stuck_created(self, *, created_before: datetime) -> int:
        """How many refunds are stuck beyond the point of safe re-initiation.

        They are excluded from the batch so they cannot starve fresher ones,
        which would leave them invisible — hence a separate count to report.
        """
        stmt = (
            select(func.count())
            .select_from(RefundModel)
            .where(
                RefundModel.status == RefundStatuses.CREATED,
                RefundModel.created_at < created_before,
            )
        )
        return (await self._session.execute(stmt)).scalar_one()

    async def update(self, refund: Refund, *, expected_status: RefundStatuses) -> None:
        # A refund transition is an atomic CAS, same as for payments.
        stmt = (
            sql_update(RefundModel)
            .where(RefundModel.id == refund.id, RefundModel.status == expected_status)
            .values(
                status=refund.status,
                failure_reason=refund.failure_reason,
                error_message=refund.error_message,
            )
        )
        result = await self._session.execute(stmt)
        cursor_result = cast("CursorResult[Any]", result)
        if cursor_result.rowcount == 0:
            model = await self._session.get(RefundModel, refund.id, populate_existing=True)
            if model is None:
                raise RefundNotFoundError
            raise StaleRefundStateError(refund.id, expected_status)

    @staticmethod
    def _to_model(refund: Refund) -> RefundModel:
        return RefundModel(
            id=refund.id,
            payment_id=refund.payment_id,
            amount=refund.amount,
            status=refund.status,
            created_at=refund.created_at,
            meta=refund.metadata,
            failure_reason=refund.failure_reason,
            error_message=refund.error_message,
        )

    @staticmethod
    def _to_domain(refund: RefundModel) -> Refund:
        return Refund(
            id=refund.id,
            payment_id=refund.payment_id,
            amount=refund.amount,
            status=refund.status,
            created_at=refund.created_at,
            metadata=refund.meta,
            failure_reason=refund.failure_reason,
            error_message=refund.error_message,
        )


class SQLAlchemyRefundIdempotencyKeyRepository:
    def __init__(self, session: AsyncSession):
        self._session = session

    async def get(self, key: str) -> RefundIdempotencyRecord | None:
        # populate_existing: the resume algorithm re-reads the key and needs a
        # fresh SELECT, not the identity-map cache.
        model = await self._session.get(RefundIdempotencyKeyModel, key, populate_existing=True)
        if model is None:
            return None
        return RefundIdempotencyRecord(
            key=model.key,
            request_hash=model.request_hash,
            refund_id=model.refund_id,
            response_body=model.response_body,
        )

    async def add(self, record: RefundIdempotencyRecord) -> None:
        self._session.add(
            RefundIdempotencyKeyModel(
                key=record.key,
                request_hash=record.request_hash,
                refund_id=record.refund_id,
                response_body=record.response_body,
            )
        )
        # flush inside the SAVEPOINT: a key conflict must surface as an
        # IntegrityError here, where the use case catches it, not at the final commit.
        await self._session.flush()

    async def set_response(self, key: str, response_body: dict[str, Any]) -> None:
        model = await self._session.get(RefundIdempotencyKeyModel, key)
        if model is None:  # the key is reserved before initiation — must not get here
            raise LookupError(f"Idempotency key {key!r} is not reserved")
        model.response_body = response_body
