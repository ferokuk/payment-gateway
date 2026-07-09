import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("API_KEY", "test-api-key")
os.environ.setdefault("CALLBACK_SECRET", "test-callback-secret")

import pytest

# Order matters: a plugin module must be registered before the modules that
# import it (session imports idempotency, client imports both), otherwise pytest
# fails to rewrite asserts in time and emits PytestAssertRewriteWarning.
pytest_plugins = [
    "tests.fixtures.payment",
    "tests.fixtures.idempotency",
    "tests.fixtures.session",
    "tests.fixtures.client",
]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
