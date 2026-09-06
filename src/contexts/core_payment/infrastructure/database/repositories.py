from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, cast
from uuid import UUID

from sqlalchemy import JSON, CursorResult, func, or_, select, text
from sqlalchemy import update as sql_update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute
from sqlalchemy.sql.elements import ColumnElement

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
    merchant_id: UUID
    key: str
    request_hash: str
    payment_id: UUID
    response_body: dict[str, Any] | None = None


@dataclass(frozen=True)
class RefundIdempotencyRecord:
    merchant_id: UUID
    key: str
    request_hash: str
    refund_id: UUID
    response_body: dict[str, Any] | None = None


@dataclass(frozen=True)
class RefundReconciliationCandidate:
    refund: Refund
    attempts: int


class _MerchantScope:
    def __init__(self, session: AsyncSession, merchant_id: UUID) -> None:
        if not isinstance(merchant_id, UUID):
            raise ValueError("A merchant UUID is required")
        self._session = session
        self._merchant_id = merchant_id

    @property
    def merchant_id(self) -> UUID:
        return self._merchant_id

    def _scope(self, column: InstrumentedAttribute[UUID]) -> tuple[ColumnElement[bool], ...]:
        return (column == self._merchant_id,)

    def _validate_owner(self, merchant_id: UUID) -> None:
        if merchant_id != self._merchant_id:
            raise ValueError("Object does not belong to this merchant scope")


class _SystemScope(_MerchantScope):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @property
    def merchant_id(self) -> UUID:
        # System authority must never become a default owner for client writes.
        raise RuntimeError("A system repository has no merchant scope")

    def _scope(self, column: InstrumentedAttribute[UUID]) -> tuple[ColumnElement[bool], ...]:
        return ()

    def _validate_owner(self, merchant_id: UUID) -> None:
        pass


class SQLAlchemyPaymentRepository(_MerchantScope):
    async def add(self, payment: Payment) -> None:
        self._validate_owner(payment.merchant_id)
        model = self._to_model(payment)
        self._session.add(model)

    async def get_by_id(self, payment_id: UUID) -> Payment | None:
        # populate_existing: a re-read after a CAS conflict must issue a fresh
        # SELECT rather than return the object from the identity map.
        model = await self._session.scalar(
            select(PaymentModel)
            .where(PaymentModel.id == payment_id, *self._scope(PaymentModel.merchant_id))
            .execution_options(populate_existing=True)
        )
        return self._to_domain(model) if model else None

    async def update(self, payment: Payment, *, expected_status: PaymentStatuses) -> None:
        self._validate_owner(payment.merchant_id)
        # A payment transition is an atomic CAS: the guard on the from-status
        # protects against overwriting state that has moved ahead.
        stmt = (
            sql_update(PaymentModel)
            .where(
                PaymentModel.id == payment.id,
                PaymentModel.merchant_id == payment.merchant_id,
                *self._scope(PaymentModel.merchant_id),
                PaymentModel.status == expected_status,
            )
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
            current = await self.get_by_id(payment.id)
            if current is None or current.merchant_id != payment.merchant_id:
                raise PaymentNotFoundError
            raise StalePaymentStateError(payment.id, expected_status)

    async def reserve_refund_amount(self, payment_id: UUID, amount: Decimal) -> None:
        # One atomic guarded UPDATE closes the check-then-act window: two
        # concurrent reservations cannot both pass the remainder check.
        stmt = (
            sql_update(PaymentModel)
            .where(
                PaymentModel.id == payment_id,
                *self._scope(PaymentModel.merchant_id),
                PaymentModel.status == PaymentStatuses.SUCCESS,
                PaymentModel.refunded_amount + amount <= PaymentModel.amount,
            )
            .values(refunded_amount=PaymentModel.refunded_amount + amount)
        )
        result = await self._session.execute(stmt)
        cursor_result = cast("CursorResult[Any]", result)
        if cursor_result.rowcount == 0:
            # Distinguish missing payment / wrong status / exhausted remainder.
            model = await self.get_by_id(payment_id)
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
                *self._scope(PaymentModel.merchant_id),
                PaymentModel.refunded_amount - amount >= 0,
            )
            .values(refunded_amount=PaymentModel.refunded_amount - amount)
        )
        result = await self._session.execute(stmt)
        cursor_result = cast("CursorResult[Any]", result)
        if cursor_result.rowcount == 0:
            model = await self.get_by_id(payment_id)
            if model is None:
                raise PaymentNotFoundError
            raise LookupError(
                f"Releasing {amount} for payment {payment_id} would make refunded_amount negative"
            )

    @staticmethod
    def _to_model(payment: Payment) -> PaymentModel:
        return PaymentModel(
            id=payment.id,
            merchant_id=payment.merchant_id,
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
            merchant_id=payment.merchant_id,
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


class SystemSQLAlchemyPaymentRepository(_SystemScope, SQLAlchemyPaymentRepository):
    """Service-only access for authenticated callbacks and reconciliation."""


class SQLAlchemyIdempotencyKeyRepository(_MerchantScope):
    async def get(self, key: str) -> IdempotencyRecord | None:
        # populate_existing: re-reading the key needs a fresh SELECT rather
        # than an object cached in the identity map (the resume algorithm).
        model = await self._session.scalar(
            select(IdempotencyKeyModel)
            .where(IdempotencyKeyModel.key == key, *self._scope(IdempotencyKeyModel.merchant_id))
            .execution_options(populate_existing=True)
        )
        if model is None:
            return None
        return IdempotencyRecord(
            merchant_id=model.merchant_id,
            key=model.key,
            request_hash=model.request_hash,
            payment_id=model.payment_id,
            response_body=model.response_body,
        )

    async def add(self, record: IdempotencyRecord) -> None:
        self._validate_owner(record.merchant_id)
        self._session.add(
            IdempotencyKeyModel(
                merchant_id=record.merchant_id,
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
        result = await self._session.execute(
            sql_update(IdempotencyKeyModel)
            .where(IdempotencyKeyModel.key == key, *self._scope(IdempotencyKeyModel.merchant_id))
            .values(response_body=response_body)
        )
        if cast("CursorResult[Any]", result).rowcount == 0:
            raise LookupError(f"Idempotency key {key!r} is not reserved")


class SQLAlchemyRefundRepository(_MerchantScope):
    async def add(self, refund: Refund) -> None:
        self._validate_owner(refund.merchant_id)
        self._session.add(self._to_model(refund))

    async def get_by_id(self, refund_id: UUID) -> Refund | None:
        # populate_existing: a re-read after a CAS conflict must issue a fresh
        # SELECT rather than return the object from the identity map.
        model = await self._session.scalar(
            select(RefundModel)
            .where(RefundModel.id == refund_id, *self._scope(RefundModel.merchant_id))
            .execution_options(populate_existing=True)
        )
        return self._to_domain(model) if model else None

    async def list_by_payment_id(self, payment_id: UUID) -> list[Refund]:
        # Ordering belongs in SQL, not in Python: the API promises creation
        # order, and id breaks ties between refunds born in the same clock tick
        # (uuid7 is time-ordered, so the tiebreaker follows the same axis).
        stmt = (
            select(RefundModel)
            .where(RefundModel.payment_id == payment_id, *self._scope(RefundModel.merchant_id))
            .order_by(RefundModel.created_at, RefundModel.id)
        )
        models = (await self._session.execute(stmt)).scalars().all()
        return [self._to_domain(model) for model in models]

    async def list_unresolved(
        self,
        *,
        statuses: Collection[RefundStatuses],
        created_before: datetime,
        due_before: datetime,
        limit: int,
    ) -> list[RefundReconciliationCandidate]:
        """Due open refunds, ordered by their scheduled check, then by id."""
        due_at = func.coalesce(RefundModel.next_reconcile_at, RefundModel.created_at)
        stmt = (
            select(RefundModel)
            .where(
                *self._scope(RefundModel.merchant_id),
                # A literal predicate also lets generic prepared plans use the
                # partial index, independently of the bound status filter.
                text("status IN ('CREATED', 'PENDING', 'ERROR')"),
                RefundModel.status.in_(statuses),
                RefundModel.created_at < created_before,
                due_at <= due_before,
            )
            .order_by(due_at, RefundModel.id)
            .limit(limit)
            .execution_options(populate_existing=True)
        )
        models = (await self._session.execute(stmt)).scalars().all()
        return [
            RefundReconciliationCandidate(self._to_domain(model), model.reconciliation_attempts)
            for model in models
        ]

    async def schedule_reconciliation(
        self, candidate: RefundReconciliationCandidate, *, next_check_at: datetime
    ) -> bool:
        """Claim this attempt and persist its retry before any provider call.

        The caller commits immediately: no lock crosses network I/O. A crashed
        worker leaves a finite delay, not a permanently claimed refund.
        """
        self._validate_owner(candidate.refund.merchant_id)
        result = await self._session.execute(
            sql_update(RefundModel)
            .where(
                RefundModel.id == candidate.refund.id,
                RefundModel.merchant_id == candidate.refund.merchant_id,
                *self._scope(RefundModel.merchant_id),
                RefundModel.status == candidate.refund.status,
                RefundModel.reconciliation_attempts == candidate.attempts,
            )
            .values(
                next_reconcile_at=next_check_at,
                reconciliation_attempts=RefundModel.reconciliation_attempts + 1,
            )
        )
        return cast("CursorResult[Any]", result).rowcount == 1

    async def update(self, refund: Refund, *, expected_status: RefundStatuses) -> None:
        self._validate_owner(refund.merchant_id)
        # A refund transition is an atomic CAS, same as for payments.
        stmt = (
            sql_update(RefundModel)
            .where(
                RefundModel.id == refund.id,
                RefundModel.merchant_id == refund.merchant_id,
                *self._scope(RefundModel.merchant_id),
                RefundModel.status == expected_status,
            )
            .values(
                status=refund.status,
                failure_reason=refund.failure_reason,
                error_message=refund.error_message,
            )
        )
        result = await self._session.execute(stmt)
        cursor_result = cast("CursorResult[Any]", result)
        if cursor_result.rowcount == 0:
            current = await self.get_by_id(refund.id)
            if current is None or current.merchant_id != refund.merchant_id:
                raise RefundNotFoundError
            raise StaleRefundStateError(refund.id, expected_status)

    @staticmethod
    def _to_model(refund: Refund) -> RefundModel:
        return RefundModel(
            id=refund.id,
            merchant_id=refund.merchant_id,
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
            merchant_id=refund.merchant_id,
            payment_id=refund.payment_id,
            amount=refund.amount,
            status=refund.status,
            created_at=refund.created_at,
            metadata=refund.meta,
            failure_reason=refund.failure_reason,
            error_message=refund.error_message,
        )


class SystemSQLAlchemyRefundRepository(_SystemScope, SQLAlchemyRefundRepository):
    """Service-only access for authenticated callbacks and reconciliation."""


class SQLAlchemyRefundIdempotencyKeyRepository(_MerchantScope):
    async def get(self, key: str) -> RefundIdempotencyRecord | None:
        # populate_existing: the resume algorithm re-reads the key and needs a
        # fresh SELECT, not the identity-map cache.
        model = await self._session.scalar(
            select(RefundIdempotencyKeyModel)
            .where(
                RefundIdempotencyKeyModel.key == key,
                *self._scope(RefundIdempotencyKeyModel.merchant_id),
            )
            .execution_options(populate_existing=True)
        )
        if model is None:
            return None
        return RefundIdempotencyRecord(
            merchant_id=model.merchant_id,
            key=model.key,
            request_hash=model.request_hash,
            refund_id=model.refund_id,
            response_body=model.response_body,
        )

    async def add(self, record: RefundIdempotencyRecord) -> None:
        self._validate_owner(record.merchant_id)
        self._session.add(
            RefundIdempotencyKeyModel(
                merchant_id=record.merchant_id,
                key=record.key,
                request_hash=record.request_hash,
                refund_id=record.refund_id,
                response_body=record.response_body,
            )
        )
        # flush inside the SAVEPOINT: a key conflict must surface as an
        # IntegrityError here, where the use case catches it, not at the final commit.
        await self._session.flush()

    async def set_response(self, key: str, response_body: dict[str, Any]) -> dict[str, Any]:
        # Recovery may race with another API response. The first snapshot must
        # remain immutable, and every contender must return that same snapshot.
        await self._session.execute(
            sql_update(RefundIdempotencyKeyModel)
            .where(
                RefundIdempotencyKeyModel.key == key,
                *self._scope(RefundIdempotencyKeyModel.merchant_id),
                or_(
                    RefundIdempotencyKeyModel.response_body.is_(None),
                    # JSONB serializes Python None as JSON null by default;
                    # existing keys may use either representation of no response.
                    RefundIdempotencyKeyModel.response_body == JSON.NULL,
                ),
            )
            .values(response_body=response_body)
        )
        record = await self.get(key)
        if record is None or record.response_body is None:
            raise LookupError(f"Idempotency key {key!r} is not reserved")
        return record.response_body
