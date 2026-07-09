from types import TracebackType
from typing import Any

import pytest
from tests.fixtures.idempotency import FakeIdempotencyKeyRepository
from tests.fixtures.payment import FakePaymentRepository


class _FakeSavepoint:
    """SAVEPOINT emulation: snapshot the stores on enter, roll back on exception."""

    def __init__(self, stores: tuple[dict[Any, Any], ...]) -> None:
        self._stores = stores
        self._snapshots: list[dict[Any, Any]] = []

    async def __aenter__(self) -> _FakeSavepoint:
        # The snapshot is shallow: rollback restores dict membership, but not
        # mutations of objects already stored. Good enough while the fake repos
        # keep copies (model_copy/replace) and domain mutations happen outside SAVEPOINT.
        self._snapshots = [dict(store) for store in self._stores]
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if exc_type is not None:
            for store, snapshot in zip(self._stores, self._snapshots, strict=True):
                store.clear()
                store.update(snapshot)


class FakeSession:
    """Minimal AsyncSession stand-in: counts commits and emulates SAVEPOINT
    on top of the fake repositories' dicts."""

    def __init__(self, *stores: dict[Any, Any]) -> None:
        self.commits = 0
        self._stores = stores

    async def commit(self) -> None:
        self.commits += 1

    async def flush(self) -> None:
        """Fake repositories write to dicts immediately — flush has nothing to do."""

    def begin_nested(self) -> _FakeSavepoint:
        return _FakeSavepoint(self._stores)


@pytest.fixture
def fake_session(
    fake_repo: FakePaymentRepository, fake_key_repo: FakeIdempotencyKeyRepository
) -> FakeSession:
    return FakeSession(fake_repo._payments, fake_key_repo._records)
