import pytest
from sqlalchemy.exc import IntegrityError
from src.contexts.core_payment.infrastructure.database.repositories import (
    RefundIdempotencyRecord,
)
from src.shared.ids import new_uuid
from tests.fixtures.merchants import MERCHANT_ID
from tests.fixtures.refund import FakeRefundIdempotencyKeyRepository


def _record(key: str = "abc") -> RefundIdempotencyRecord:
    return RefundIdempotencyRecord(
        merchant_id=MERCHANT_ID, key=key, request_hash="0" * 64, refund_id=new_uuid()
    )


@pytest.mark.anyio
async def test_get_returns_none_for_missing_key() -> None:
    repo = FakeRefundIdempotencyKeyRepository()

    assert await repo.get("missing") is None


@pytest.mark.anyio
async def test_add_then_get_returns_record() -> None:
    repo = FakeRefundIdempotencyKeyRepository()
    record = _record()

    await repo.add(record)

    stored = await repo.get("abc")
    assert stored == record
    assert stored is not None
    assert stored.response_body is None


@pytest.mark.anyio
async def test_add_duplicate_key_raises_integrity_error() -> None:
    repo = FakeRefundIdempotencyKeyRepository()
    await repo.add(_record())

    with pytest.raises(IntegrityError):
        await repo.add(_record())


@pytest.mark.anyio
async def test_set_response_updates_record() -> None:
    repo = FakeRefundIdempotencyKeyRepository()
    await repo.add(_record())

    await repo.set_response("abc", {"status": "pending"})

    stored = await repo.get("abc")
    assert stored is not None
    assert stored.response_body == {"status": "pending"}


@pytest.mark.anyio
async def test_set_response_for_missing_key_raises() -> None:
    repo = FakeRefundIdempotencyKeyRepository()

    with pytest.raises(LookupError):
        await repo.set_response("missing", {})


@pytest.mark.anyio
async def test_response_is_write_once_and_returns_the_winner() -> None:
    repo = FakeRefundIdempotencyKeyRepository()
    await repo.add(_record())
    first = await repo.set_response("abc", {"status": "pending"})
    second = await repo.set_response("abc", {"status": "success"})
    assert first == second == {"status": "pending"}
    stored = await repo.get("abc")
    assert stored is not None and stored.response_body == first
