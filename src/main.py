from contextlib import asynccontextmanager
from dishka import make_async_container
from dishka.integrations.fastapi import FromDishka, inject, setup_dishka
from sqlalchemy import text
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from src.contexts.core_payment.ioc import CorePaymentProvider
from src.contexts.core_payment.presentation.routers.payment import router as payment_router
from src.shared.ioc import ConfigProvider, DatabaseProvider


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await app.state.dishka_container.close()


app = FastAPI(title="Payment Gateway", lifespan=lifespan)

app.include_router(payment_router)

container = make_async_container(
    ConfigProvider(),
    DatabaseProvider(),
    CorePaymentProvider(),
)
setup_dishka(container, app)


@app.get("/health")
@inject
async def health(session: FromDishka[AsyncSession]) -> dict:
    result = await session.execute(text("SELECT 1"))
    return {"status": "ok", "db": result.scalar()}
