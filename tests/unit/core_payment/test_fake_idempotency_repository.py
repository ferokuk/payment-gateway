import pytest
from sqlalchemy.exc import IntegrityError
from src.contexts.core_payment.infrastructure.database.repositories import (
    IdempotencyRecord,
)
from src.shared.ids import new_uuid
from tests.fixtures.idempotency import FakeIdempotencyKeyRepository
from tests.fixtures.merchants import MERCHANT_ID


def _record(key: str = "abc") -> IdempotencyRecord:
    return IdempotencyRecord(
        merchant_id=MERCHANT_ID, key=key, request_hash="0" * 64, payment_id=new_uuid()
    )


@pytest.mark.anyio
async def test_get_returns_none_for_missing_key() -> None:
    repo = FakeIdempotencyKeyRepository()

    assert await repo.get("missing") is None


@pytest.mark.anyio
async def test_add_then_get_returns_record() -> None:
    repo = FakeIdempotencyKeyRepository()
    record = _record()

    await repo.add(record)

    stored = await repo.get("abc")
    assert stored == record
    assert stored is not None
    assert stored.response_body is None


@pytest.mark.anyio
async def test_add_duplicate_key_raises_integrity_error() -> None:
    repo = FakeIdempotencyKeyRepository()
    await repo.add(_record())

    with pytest.raises(IntegrityError):
        await repo.add(_record())


@pytest.mark.anyio
async def test_set_response_updates_record() -> None:
    repo = FakeIdempotencyKeyRepository()
    await repo.add(_record())

    await repo.set_response("abc", {"status": "pending"})

    stored = await repo.get("abc")
    assert stored is not None
    assert stored.response_body == {"status": "pending"}


@pytest.mark.anyio
async def test_set_response_for_missing_key_raises() -> None:
    repo = FakeIdempotencyKeyRepository()

    with pytest.raises(LookupError):
        await repo.set_response("missing", {})
