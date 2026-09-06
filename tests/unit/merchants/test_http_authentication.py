import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import UUID

import pytest
from dishka import Provider, Scope, make_async_container, provide
from dishka.integrations.fastapi import DishkaRoute, FastapiProvider, FromDishka, setup_dishka
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, ConnectError, MockTransport, Request, Response
from src.contexts.merchants.application.public import MerchantIdentity
from src.shared import security
from src.shared.config import Settings
from src.shared.merchant_client import HTTPMerchantAuthenticator, MerchantServiceUnavailable
from src.shared.security import AuthProvider, CallbackAuthenticated

pytestmark = pytest.mark.anyio

MERCHANT_ID = UUID("00000000-0000-4000-8000-000000000001")
API_KEY_ID = UUID("00000000-0000-4000-8000-000000000002")
SERVICE_SECRET = "internal-service-secret"
IDENTITY = {"merchant_id": str(MERCHANT_ID), "api_key_id": str(API_KEY_ID)}


async def test_internal_request_transmits_only_api_key_and_service_credentials() -> None:
    requests: list[Request] = []

    def respond(request: Request) -> Response:
        requests.append(request)
        return Response(200, json=IDENTITY)

    async with AsyncClient(
        transport=MockTransport(respond), base_url="http://merchant:8001"
    ) as client:
        identity = await HTTPMerchantAuthenticator(client, SERVICE_SECRET)("payment-api-key")

    assert identity == MerchantIdentity(MERCHANT_ID, API_KEY_ID)
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert str(request.url) == "http://merchant:8001/internal/authenticate"
    assert json.loads(request.content) == {"api_key": "payment-api-key"}
    assert request.headers["X-Service-Secret"] == SERVICE_SECRET
    assert "authorization" not in request.headers
    assert "x-api-key" not in request.headers


@pytest.mark.parametrize("api_key", [None, "", "x" * 4097], ids=["missing", "empty", "oversized"])
async def test_unusable_api_key_never_calls_service_even_when_unconfigured(
    api_key: str | None,
) -> None:
    def fail_on_request(request: Request) -> Response:
        pytest.fail("Unusable API keys must not trigger an HTTP request")

    async with AsyncClient(
        transport=MockTransport(fail_on_request), base_url="http://merchant:8001"
    ) as client:
        assert await HTTPMerchantAuthenticator(client, "")(api_key) is None


async def test_unconfigured_service_secret_never_sends_payment_credential() -> None:
    def fail_on_request(request: Request) -> Response:
        pytest.fail("An unconfigured service must not receive the API key")

    async with AsyncClient(
        transport=MockTransport(fail_on_request), base_url="http://merchant:8001"
    ) as client:
        with pytest.raises(MerchantServiceUnavailable, match="authentication is unavailable"):
            await HTTPMerchantAuthenticator(client, "")("payment-api-key")


@pytest.mark.parametrize("status_code", [401, 403])
async def test_invalid_credentials_are_rejected(status_code: int) -> None:
    async with AsyncClient(
        transport=MockTransport(lambda request: Response(status_code)),
        base_url="http://merchant:8001",
    ) as client:
        assert await HTTPMerchantAuthenticator(client, SERVICE_SECRET)("invalid") is None


@pytest.mark.parametrize("status_code", [201, 204, 301, 307, 400, 404, 422, 429, 500, 503])
async def test_unexpected_status_never_becomes_an_identity(status_code: int) -> None:
    async with AsyncClient(
        transport=MockTransport(lambda request: Response(status_code, json=IDENTITY)),
        base_url="http://merchant:8001",
    ) as client:
        with pytest.raises(MerchantServiceUnavailable):
            await HTTPMerchantAuthenticator(client, SERVICE_SECRET)("payment-api-key")


@pytest.mark.parametrize(
    "response_body",
    [
        b"not-json",
        b"null",
        b"[]",
        b"{}",
        b'{"merchant_id":null,"api_key_id":null}',
        b'{"merchant_id":"bad","api_key_id":"bad"}',
        json.dumps({"merchant_id": str(MERCHANT_ID)}).encode(),
    ],
)
async def test_malformed_success_is_unavailable(response_body: bytes) -> None:
    async with AsyncClient(
        transport=MockTransport(lambda request: Response(200, content=response_body)),
        base_url="http://merchant:8001",
    ) as client:
        with pytest.raises(MerchantServiceUnavailable) as error:
            await HTTPMerchantAuthenticator(client, SERVICE_SECRET)("payment-api-key")
    assert str(error.value) == "Merchant authentication is unavailable"


async def test_network_failure_does_not_expose_credential_or_upstream_error() -> None:
    def unavailable(request: Request) -> Response:
        raise ConnectError("upstream diagnostics with payment-api-key", request=request)

    async with AsyncClient(
        transport=MockTransport(unavailable), base_url="http://merchant:8001"
    ) as client:
        with pytest.raises(MerchantServiceUnavailable) as error:
            await HTTPMerchantAuthenticator(client, SERVICE_SECRET)("payment-api-key")
    assert str(error.value) == "Merchant authentication is unavailable"
    assert error.value.__suppress_context__


class _Config(Provider):
    @provide(scope=Scope.APP)
    def settings(self) -> Settings:
        return Settings(
            database_url="postgresql+asyncpg://unused:unused@localhost/unused",
            callback_secret="callback-secret",
            merchant_service_url="http://merchant:8001",
            merchant_service_secret=SERVICE_SECRET,
        )


@dataclass
class Gateway:
    client: AsyncClient
    merchant_requests: list[Request]


@pytest.fixture
async def gateway(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Gateway]:
    merchant_requests: list[Request] = []
    upstream_clients: list[AsyncClient] = []

    def respond(request: Request) -> Response:
        merchant_requests.append(request)
        token = json.loads(request.content)["api_key"]
        if token == "payment-api-key":
            return Response(200, json=IDENTITY)
        if token == "upstream-error":
            return Response(500, json={"detail": "private diagnostic"})
        return Response(401)

    def upstream_client(*, base_url: str, timeout: float, follow_redirects: bool) -> AsyncClient:
        assert base_url == "http://merchant:8001"
        assert 0 < timeout <= 5
        assert follow_redirects is False
        client = AsyncClient(
            transport=MockTransport(respond),
            base_url=base_url,
            timeout=timeout,
            follow_redirects=follow_redirects,
        )
        upstream_clients.append(client)
        return client

    monkeypatch.setattr(security, "AsyncClient", upstream_client)
    app = FastAPI()
    app.router.route_class = DishkaRoute

    @app.get("/identity")
    async def identity(identity: FromDishka[MerchantIdentity]) -> dict[str, str]:
        return {"merchant_id": str(identity.merchant_id), "api_key_id": str(identity.api_key_id)}

    @app.post("/callback")
    async def callback(identity: FromDishka[CallbackAuthenticated]) -> dict[str, bool]:
        return {"accepted": True}

    # No database provider or session: payment auth only depends on HTTP.
    container = make_async_container(_Config(), AuthProvider(), FastapiProvider())
    setup_dishka(container, app)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield Gateway(client, merchant_requests)
        assert len(upstream_clients) <= 1
    finally:
        await container.close()
    assert all(client.is_closed for client in upstream_clients)


async def test_payment_gateway_uses_remote_identity_and_reuses_http_client(
    gateway: Gateway,
) -> None:
    for _ in range(2):
        response = await gateway.client.get(
            "/identity",
            headers={"X-API-Key": "payment-api-key", "X-Merchant-ID": str(API_KEY_ID)},
        )
        assert response.status_code == 200
        assert response.json() == IDENTITY
    assert len(gateway.merchant_requests) == 2


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"X-API-Key": ""},
        {"X-API-Key": "x" * 4097},
        {"Authorization": "Bearer profile-session-token"},
    ],
    ids=["missing", "empty", "oversized", "profile-bearer"],
)
async def test_payment_auth_requires_api_key_header(
    gateway: Gateway, headers: dict[str, str]
) -> None:
    response = await gateway.client.get("/identity", headers=headers)
    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid or missing API key"}
    assert gateway.merchant_requests == []


async def test_profile_session_does_not_grant_payment_access(gateway: Gateway) -> None:
    response = await gateway.client.get("/identity", headers={"X-API-Key": "profile-session-token"})
    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid or missing API key"}


async def test_payment_auth_reports_upstream_failure_as_generic_503(gateway: Gateway) -> None:
    response = await gateway.client.get("/identity", headers={"X-API-Key": "upstream-error"})
    assert response.status_code == 503
    assert response.json() == {"detail": "Merchant authentication is unavailable"}


async def test_callback_auth_is_independent_of_merchant_service(gateway: Gateway) -> None:
    accepted = await gateway.client.post(
        "/callback", headers={"X-Callback-Secret": "callback-secret"}
    )
    assert accepted.status_code == 200
    denied = await gateway.client.post("/callback", headers={"X-API-Key": "payment-api-key"})
    assert denied.status_code == 401
    assert gateway.merchant_requests == []


def test_service_secret_is_hidden_in_settings_representation() -> None:
    assert SERVICE_SECRET not in repr(_Config().settings())
