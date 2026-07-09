import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("API_KEY", "test-api-key")
os.environ.setdefault("CALLBACK_SECRET", "test-callback-secret")

import pytest
from src.shared.logging import configure_logging

# Unconfigured structlog falls back to ConsoleRenderer with rich tracebacks
# (rich comes transitively with fastapi[standard]): rendering one exception
# with locals costs hundreds of milliseconds, and tests that await background
# tasks under wait_for start racing their timeouts on slow CI runners.
# JSON mode renders exceptions as plain text — cheap and deterministic.
configure_logging(json_logs=True)

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
