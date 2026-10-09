from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from src.contexts.merchants.application.service import (
    MerchantConflict,
    MerchantInvalidRequest,
    MerchantNotFound,
    MerchantUnauthorized,
)
from src.contexts.merchants.configuration import MerchantSettings
from src.contexts.merchants.infrastructure.secrets import SecretVault
from src.contexts.merchants.presentation.router import router
from src.shared.database.engine import create_engine, create_sessionmaker
from starlette.responses import Response


def create_app(settings: MerchantSettings | None = None) -> FastAPI:
    configuration = settings if settings is not None else MerchantSettings()
    vault = SecretVault(configuration.encryption_key.get_secret_value())
    engine = create_engine(configuration.database_url)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        await engine.dispose()

    app = FastAPI(title="Merchant Service", lifespan=lifespan)
    app.state.settings = configuration
    app.state.vault = vault
    app.state.sessionmaker = create_sessionmaker(engine)
    app.include_router(router)

    @app.middleware("http")
    async def prevent_caching(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Pydantic inputs and contexts can contain plaintext credentials, even
        # when the corresponding field uses SecretStr.
        errors = [
            {"type": error["type"], "loc": error["loc"], "msg": error["msg"]}
            for error in exc.errors()
        ]
        return JSONResponse(status_code=422, content={"detail": errors})

    async def merchant_error(request: Request, exc: Exception) -> JSONResponse:
        status_codes: dict[type[Exception], int] = {
            MerchantNotFound: 404,
            MerchantConflict: 409,
            MerchantUnauthorized: 401,
            MerchantInvalidRequest: 422,
        }
        headers = {"WWW-Authenticate": "Bearer"} if isinstance(exc, MerchantUnauthorized) else None
        return JSONResponse(
            status_code=status_codes[type(exc)], content={"detail": str(exc)}, headers=headers
        )

    for exception_type in (
        MerchantNotFound,
        MerchantConflict,
        MerchantUnauthorized,
        MerchantInvalidRequest,
    ):
        app.add_exception_handler(exception_type, merchant_error)

    return app
