from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from dishka import make_async_container
from dishka.integrations.fastapi import FastapiProvider, FromDishka, inject, setup_dishka
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.contexts.core_payment.ioc import CorePaymentProvider
from src.contexts.core_payment.presentation.routers.payment import router as payment_router
from src.shared.config import settings
from src.shared.ioc import ConfigProvider, DatabaseProvider, RepositoriesProvider
from src.shared.logging import configure_logging
from src.shared.security import AuthProvider


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    yield
    await app.state.dishka_container.close()


configure_logging(json_logs=not settings.is_debug)

app = FastAPI(title="Payment Gateway", lifespan=lifespan)

app.include_router(payment_router)

container = make_async_container(
    ConfigProvider(),
    DatabaseProvider(),
    CorePaymentProvider(),
    AuthProvider(),
    FastapiProvider(),
    RepositoriesProvider(),
)
setup_dishka(container, app)


@app.get("/health")
@inject
async def health(session: FromDishka[AsyncSession]) -> dict[str, Any]:
    result = await session.execute(text("SELECT 1"))
    return {"status": "ok", "db": result.scalar()}
