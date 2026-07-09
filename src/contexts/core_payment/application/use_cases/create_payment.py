import hashlib
import json
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.contexts.core_payment.application.dto.payment import (
    CreatePaymentInputDTO,
    CreatePaymentOutputDTO,
)
from src.contexts.core_payment.domain.exceptions import (
    IdempotencyKeyMismatchError,
    PaymentNotFoundError,
    StalePaymentStateError,
    UnknownProviderError,
)
from src.contexts.core_payment.domain.payment import Payment
from src.contexts.core_payment.domain.statuses import PaymentStatuses
from src.contexts.core_payment.infrastructure.database.repositories import (
    IdempotencyRecord,
    SQLAlchemyIdempotencyKeyRepository,
    SQLAlchemyPaymentRepository,
)
from src.contexts.core_payment.infrastructure.providers.base import (
    FAKE_PROVIDER_ID,
    PaymentProvider,
)
from src.shared.ids import new_uuid


def _request_hash(command: CreatePaymentInputDTO) -> str:
    """SHA-256 of the canonicalized body: one key — one body."""
    canonical = json.dumps(command.model_dump(mode="json"), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _replay(record: IdempotencyRecord) -> CreatePaymentOutputDTO:
    assert record.response_body is not None  # checked by the caller
    return CreatePaymentOutputDTO.model_validate({**record.response_body, "replayed": True})


class CreatePaymentUseCase:
    """Creates a payment in two short transactions
    with idempotency via Idempotency-Key (Stripe model).

    Txn1 commits the payment in CREATED (and reserves the key) before
    contacting the provider. The provider call happens outside an open
    transaction. Txn2 (autobegin) moves the payment to PENDING and atomically
    stores the response under the key; the final commit is done by the DI
    session provider when the request exits.
    Invariant: response_body IS NULL <=> the payment is stuck in CREATED.
    """

    def __init__(
        self,
        payment_repository: SQLAlchemyPaymentRepository,
        idempotency_repository: SQLAlchemyIdempotencyKeyRepository,
        payment_provider: PaymentProvider,
        session: AsyncSession,
    ) -> None:
        self._payment_repository = payment_repository
        self._idempotency_repository = idempotency_repository
        self._payment_provider = payment_provider
        self._session = session

    async def __call__(
        self,
        command: CreatePaymentInputDTO,
        idempotency_key: str | None = None,
    ) -> CreatePaymentOutputDTO:
        if command.provider_id != FAKE_PROVIDER_ID:
            raise UnknownProviderError(command.provider_id)

        if idempotency_key is None:
            payment = self._build_payment(command)
            await self._payment_repository.add(payment)
            return await self._initiate_and_finalize(payment, idempotency_key=None)

        record = await self._idempotency_repository.get(idempotency_key)
        if record is None:
            payment = self._build_payment(command)
            try:
                # SAVEPOINT: on a race for the key, the duplicate payment is
                # rolled back together with the key insert.
                async with self._session.begin_nested():
                    await self._payment_repository.add(payment)
                    # Without relationship() the unit of work does not order
                    # INSERTs across mappers: the payment must be flushed before
                    # inserting the key, otherwise the FK idempotency_keys.payment_id
                    # points at a nonexistent row (caught by a real-DB test of
                    # the transactional mechanics).
                    await self._session.flush()
                    await self._idempotency_repository.add(
                        IdempotencyRecord(
                            key=idempotency_key,
                            request_hash=_request_hash(command),
                            payment_id=payment.id,
                        )
                    )
            except IntegrityError:
                record = await self._idempotency_repository.get(idempotency_key)
                if record is None:  # race with key deletion — outside the model
                    raise
            else:
                return await self._initiate_and_finalize(payment, idempotency_key)

        if record.request_hash != _request_hash(command):
            raise IdempotencyKeyMismatchError(idempotency_key)
        if record.response_body is not None:
            return _replay(record)

        # Resuming an interrupted operation: the key exists but has no response.
        found_payment = await self._payment_repository.get_by_id(record.payment_id)
        if found_payment is None:
            raise PaymentNotFoundError
        if found_payment.status is PaymentStatuses.CREATED:
            return await self._initiate_and_finalize(found_payment, idempotency_key)

        # Someone else finished the operation: PENDING and response are written
        # atomically in Txn2, so a fresh read of the key must see the response.
        record = await self._idempotency_repository.get(idempotency_key)
        if record is None or record.response_body is None:
            raise LookupError(f"Idempotency key {idempotency_key!r} lost its response")
        return _replay(record)

    @staticmethod
    def _build_payment(command: CreatePaymentInputDTO) -> Payment:
        return Payment(
            id=new_uuid(),
            amount=command.amount,
            currency=command.currency,
            status=PaymentStatuses.CREATED,
            provider_id=command.provider_id,
            created_at=datetime.now(UTC),
            metadata=command.metadata,
        )

    async def _initiate_and_finalize(
        self, payment: Payment, idempotency_key: str | None
    ) -> CreatePaymentOutputDTO:
        # Txn1: close the transaction before the external call — the attempt
        # is recorded, the connection is returned to the pool.
        await self._session.commit()

        # On ProviderInitiationError the payment stays in CREATED (the router returns 502).
        await self._payment_provider.initiate_payment(payment)

        payment.mark_pending()
        try:
            await self._payment_repository.update(payment, expected_status=PaymentStatuses.CREATED)
        except StalePaymentStateError:
            if idempotency_key is None:
                # Without a key there can be no competitors: the payment id is
                # new and callbacks never move CREATED — this violates the invariant.
                raise
            # A concurrent resume finished first: its Txn2 atomically wrote
            # PENDING + response — return its response, don't write our own.
            record = await self._idempotency_repository.get(idempotency_key)
            if record is None or record.response_body is None:
                raise LookupError(
                    f"Idempotency key {idempotency_key!r} lost its response"
                ) from None
            return _replay(record)
        dto = CreatePaymentOutputDTO(
            payment_id=payment.id,
            amount=payment.amount,
            currency=payment.currency,
            status=payment.status,
            created_at=payment.created_at,
        )
        if idempotency_key is not None:
            # exclude=True on replayed: the field is left out of the response snapshot.
            await self._idempotency_repository.set_response(
                idempotency_key, dto.model_dump(mode="json")
            )
        return dto
