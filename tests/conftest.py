import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("API_KEY", "test-api-key")

import pytest

pytest_plugins = ["tests.fixtures.payment", "tests.fixtures.client"]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
