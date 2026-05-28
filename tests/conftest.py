import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("API_KEY", "test-api-key")

import pytest


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
