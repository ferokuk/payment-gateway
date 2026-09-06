import secrets
from collections.abc import AsyncIterator
from typing import Annotated, Any, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from src.contexts.merchants.application.service import MerchantService
from src.contexts.merchants.configuration import MerchantSettings
from src.contexts.merchants.infrastructure.secrets import SecretVault
from src.contexts.merchants.presentation.schemas import (
    AuthenticateAPIKeyRequest,
    LoginRequest,
    MerchantConfigurationResponse,
    MerchantIdentityResponse,
    PatchProfileRequest,
    PatchSecretsRequest,
    ProfileResponse,
    RegisterMerchantRequest,
    TokenResponse,
)

router = APIRouter()
bearer = HTTPBearer(auto_error=False)


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    factory = cast("async_sessionmaker[AsyncSession]", request.app.state.sessionmaker)
    async with factory() as session:
        try:
            yield session
        except BaseException:
            await session.rollback()
            raise


def get_settings(request: Request) -> MerchantSettings:
    return cast("MerchantSettings", request.app.state.settings)


def get_service(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[MerchantSettings, Depends(get_settings)],
) -> MerchantService:
    vault = cast("SecretVault", request.app.state.vault)
    return MerchantService(session, vault, session_ttl_seconds=settings.session_ttl_seconds)


Service = Annotated[MerchantService, Depends(get_service)]


async def current_merchant(
    service: Service,
    authorization: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> UUID:
    token = authorization.credentials if authorization is not None else None
    return await service.authenticate_session(token)


CurrentMerchant = Annotated[UUID, Depends(current_merchant)]


def authorize_support(
    settings: Annotated[MerchantSettings, Depends(get_settings)],
    supplied_secret: Annotated[str | None, Header(alias="X-Support-Secret")] = None,
) -> None:
    expected = settings.support_secret.get_secret_value()
    if supplied_secret is None or not secrets.compare_digest(
        supplied_secret.encode("utf-8"), expected.encode("utf-8")
    ):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")


def authorize_service(
    settings: Annotated[MerchantSettings, Depends(get_settings)],
    supplied_secret: Annotated[str | None, Header(alias="X-Service-Secret")] = None,
) -> None:
    expected = settings.service_secret.get_secret_value()
    if supplied_secret is None or not secrets.compare_digest(
        supplied_secret.encode("utf-8"), expected.encode("utf-8")
    ):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")


@router.post("/merchants", response_model=ProfileResponse, status_code=status.HTTP_201_CREATED)
async def register_merchant(payload: RegisterMerchantRequest, service: Service) -> dict[str, Any]:
    return await service.register(
        email=str(payload.email),
        name=payload.name,
        password=payload.password.get_secret_value(),
        provider_name=payload.provider_name,
        provider_secret_key=payload.provider_secret_key.get_secret_value(),
    )


@router.post("/auth/token", response_model=TokenResponse)
async def login(payload: LoginRequest, service: Service) -> dict[str, Any]:
    return await service.login(str(payload.email), payload.password.get_secret_value())


@router.get("/profile", response_model=ProfileResponse)
async def get_profile(merchant_id: CurrentMerchant, service: Service) -> dict[str, Any]:
    return await service.get_profile(merchant_id)


@router.patch("/profile", response_model=ProfileResponse)
async def update_profile(
    payload: PatchProfileRequest, merchant_id: CurrentMerchant, service: Service
) -> dict[str, Any]:
    changes = payload.model_dump(exclude_unset=True)
    if payload.provider_secret_key is not None:
        changes["provider_secret_key"] = payload.provider_secret_key.get_secret_value()
    if payload.webhook_url is not None:
        changes["webhook_url"] = str(payload.webhook_url)
    return await service.update_profile(merchant_id, changes)


@router.patch("/profile/secrets", response_model=ProfileResponse)
async def rotate_secrets(
    payload: PatchSecretsRequest, merchant_id: CurrentMerchant, service: Service
) -> dict[str, Any]:
    return await service.rotate_secrets(
        merchant_id,
        rotate_api_key=payload.rotate_api_key,
        provider_secret_key=(
            payload.provider_secret_key.get_secret_value()
            if payload.provider_secret_key is not None
            else None
        ),
        provider_name=payload.provider_name,
        password=payload.password.get_secret_value() if payload.password is not None else None,
    )


@router.delete(
    "/profile", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(authorize_support)]
)
async def delete_profile(merchant_id: UUID, service: Service) -> Response:
    await service.delete(merchant_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/internal/authenticate",
    response_model=MerchantIdentityResponse,
    dependencies=[Depends(authorize_service)],
)
async def authenticate_api_key(
    payload: AuthenticateAPIKeyRequest, service: Service
) -> MerchantIdentityResponse:
    identity = await service.authenticate_api_key(payload.api_key.get_secret_value())
    if identity is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid API key")
    return MerchantIdentityResponse(
        merchant_id=identity.merchant_id, api_key_id=identity.api_key_id
    )


@router.get(
    "/internal/merchants/{merchant_id}/configuration",
    response_model=MerchantConfigurationResponse,
    dependencies=[Depends(authorize_service)],
)
async def get_configuration(merchant_id: UUID, service: Service) -> dict[str, Any]:
    return await service.get_configuration(merchant_id)


@router.get("/health")
async def health(session: Annotated[AsyncSession, Depends(get_session)]) -> dict[str, Any]:
    result = await session.execute(text("SELECT 1"))
    return {"status": "ok", "db": result.scalar()}
