"""HTTP client and wave-run harness for the api_lab.ipynb notebook.

Reusable code lives here in a plain .py file where ruff and diffs can see it,
keeping the notebook to thin scenario cells. A new endpoint is one method
in GatewayClient plus one cell in the notebook.
"""

import asyncio
import sys
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import TracebackType
from typing import Any, Literal

import httpx
import pandas as pd

ProviderMode = Literal["manual", "auto"]

# Resolved once at import: the reconciliation helper needs the repository root
# on sys.path, and touching the filesystem inside a coroutine would block it.
_REPO_ROOT = Path(__file__).resolve().parent.parent

# Terminal payment statuses — no transitions out of them.
TERMINAL_STATUSES = frozenset({"success", "error", "failed"})

# Scenario -> callback chain. Mirrors the service's auto-provider scenarios;
# in manual mode the notebook sends the same chain via POST /callbacks/payments.
SCENARIO_CALLBACKS: dict[str, list[dict[str, str]]] = {
    "success": [{"status": "processing"}, {"status": "success"}],
    "insufficient_funds": [
        {"status": "processing"},
        {"status": "failed", "failure_reason": "insufficient_funds"},
    ],
    "fraud": [
        {"status": "processing"},
        {"status": "failed", "failure_reason": "fraud"},
    ],
    "limit_exceeded": [
        {"status": "processing"},
        {"status": "failed", "failure_reason": "limit_exceeded"},
    ],
    "timeout": [{"status": "failed", "failure_reason": "timeout"}],
    "error": [
        {"status": "processing"},
        {"status": "error", "error_message": "Internal provider error"},
    ],
}

# Expected terminal status per scenario — used for verification in summaries.
SCENARIO_EXPECTED_FINAL: dict[str, str] = {
    "success": "success",
    "insufficient_funds": "failed",
    "fraud": "failed",
    "limit_exceeded": "failed",
    "timeout": "failed",
    "error": "error",
}

# Refund scenario -> callback chain. A refund has no processing step: at real
# PSPs it is a single-step operation (Stripe: pending -> succeeded/failed).
REFUND_SCENARIO_CALLBACKS: dict[str, list[dict[str, str]]] = {
    "success": [{"status": "success"}],
    "card_unavailable": [{"status": "failed", "failure_reason": "card_unavailable"}],
    "insufficient_merchant_balance": [
        {"status": "failed", "failure_reason": "insufficient_merchant_balance"}
    ],
    "timeout": [{"status": "failed", "failure_reason": "timeout"}],
    "error": [{"status": "error", "error_message": "Internal provider error"}],
}

REFUND_SCENARIO_EXPECTED_FINAL: dict[str, str] = {
    "success": "success",
    "card_unavailable": "failed",
    "insufficient_merchant_balance": "failed",
    "timeout": "failed",
    "error": "error",
}


def scenario_for(index: int) -> str:
    """Deterministic round-robin scenario assignment by request index."""
    names = list(SCENARIO_CALLBACKS)
    return names[index % len(names)]


@dataclass(frozen=True)
class GatewayConfig:
    """Connection settings. Supply a key issued by the merchant operator CLI."""

    base_url: str = "http://localhost:8000"
    api_key: str = field(default="", repr=False)
    callback_secret: str = field(default="local-dev-callback-secret", repr=False)
    # Only the reconciliation section needs it: that job lives behind the API,
    # not in front of it. The host port comes from docker-compose.
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/payment_gateway"
    # A wave of 1000 requests saturates the service's DB pool: the tail waits
    # noticeably longer than httpx's default 5 seconds.
    timeout_seconds: float = 60.0
    # httpx's default limit (100 connections) would choke a wave of 1000
    # concurrent requests.
    max_connections: int = 1100


@dataclass(frozen=True)
class RequestResult:
    """Result of a single HTTP call.

    ok=False means no HTTP response arrived at all (timeout, dropped
    connection); details are in error. A response with any code, including
    4xx/5xx, is ok=True: for negative checks an "expected 409" is a
    successful observation.
    """

    ok: bool
    status_code: int | None
    elapsed_ms: float
    data: Any
    headers: dict[str, str]
    error: str | None

    @property
    def payment_id(self) -> str | None:
        """Payment ID from the response body, if present."""
        if isinstance(self.data, dict) and self.data.get("payment_id") is not None:
            return str(self.data["payment_id"])
        return None

    @property
    def refund_id(self) -> str | None:
        """Refund ID from the response body, if present."""
        if isinstance(self.data, dict) and self.data.get("refund_id") is not None:
            return str(self.data["refund_id"])
        return None


class GatewayClient:
    """Thin wrapper around httpx.AsyncClient for the payment gateway API.

    Methods do not raise: network errors are captured in RequestResult.error
    so that a single failed coroutine does not bring down a gather wave.
    """

    def __init__(self, config: GatewayConfig | None = None) -> None:
        self.config = config or GatewayConfig()
        limits = httpx.Limits(
            max_connections=self.config.max_connections,
            max_keepalive_connections=self.config.max_connections,
        )
        self._http = httpx.AsyncClient(
            base_url=self.config.base_url,
            timeout=self.config.timeout_seconds,
            limits=limits,
        )

    async def __aenter__(self) -> GatewayClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def health(self) -> RequestResult:
        return await self._request("GET", "/health")

    async def create_payment(
        self,
        *,
        amount: str = "10.00",
        currency: str = "USD",
        provider_id: int = 1,
        metadata: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> RequestResult:
        headers = {"X-API-Key": self.config.api_key}
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        # amount as a string: Decimal is not JSON-serializable, and the
        # service coerces the string to Decimal during validation.
        body = {
            "amount": amount,
            "currency": currency,
            "provider_id": provider_id,
            "metadata": metadata,
        }
        return await self._request("POST", "/payments", headers=headers, json_body=body)

    async def get_payment(self, payment_id: str) -> RequestResult:
        return await self._request(
            "GET", f"/payments/{payment_id}", headers={"X-API-Key": self.config.api_key}
        )

    async def send_callback(
        self,
        payment_id: str,
        status: str,
        *,
        failure_reason: str | None = None,
        error_message: str | None = None,
    ) -> RequestResult:
        body: dict[str, Any] = {"payment_id": payment_id, "status": status}
        if failure_reason is not None:
            body["failure_reason"] = failure_reason
        if error_message is not None:
            body["error_message"] = error_message
        return await self._request(
            "POST",
            "/callbacks/payments",
            headers={"X-Callback-Secret": self.config.callback_secret},
            json_body=body,
        )

    async def create_refund(
        self,
        payment_id: str,
        *,
        amount: str = "10.00",
        metadata: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> RequestResult:
        headers = {"X-API-Key": self.config.api_key}
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        return await self._request(
            "POST",
            f"/payments/{payment_id}/refunds",
            headers=headers,
            json_body={"amount": amount, "metadata": metadata},
        )

    async def get_refund(self, refund_id: str) -> RequestResult:
        return await self._request(
            "GET", f"/refunds/{refund_id}", headers={"X-API-Key": self.config.api_key}
        )

    async def list_refunds(self, payment_id: str) -> RequestResult:
        return await self._request(
            "GET", f"/payments/{payment_id}/refunds", headers={"X-API-Key": self.config.api_key}
        )

    async def send_refund_callback(
        self,
        refund_id: str,
        status: str,
        *,
        failure_reason: str | None = None,
        error_message: str | None = None,
    ) -> RequestResult:
        body: dict[str, Any] = {"refund_id": refund_id, "status": status}
        if failure_reason is not None:
            body["failure_reason"] = failure_reason
        if error_message is not None:
            body["error_message"] = error_message
        return await self._request(
            "POST",
            "/callbacks/refunds",
            headers={"X-Callback-Secret": self.config.callback_secret},
            json_body=body,
        )

    async def _request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json_body: Any = None,
    ) -> RequestResult:
        start = time.perf_counter()
        try:
            response = await self._http.request(method, url, headers=headers, json=json_body)
        except httpx.HTTPError as exc:
            return RequestResult(
                ok=False,
                status_code=None,
                elapsed_ms=(time.perf_counter() - start) * 1000,
                data=None,
                headers={},
                error=f"{type(exc).__name__}: {exc}",
            )
        elapsed_ms = (time.perf_counter() - start) * 1000
        try:
            data = response.json()
        except ValueError:
            data = None
        return RequestResult(
            ok=True,
            status_code=response.status_code,
            elapsed_ms=elapsed_ms,
            data=data,
            headers=dict(response.headers),
            error=None,
        )


@dataclass(frozen=True)
class WaveReport[T]:
    """Outcome of one wave: the results and the wall time of the whole batch."""

    label: str
    results: list[T]
    wall_time_s: float


async def run_wave[T](
    n: int,
    factory: Callable[[int], Awaitable[T]],
    *,
    label: str | None = None,
    concurrency: int | None = None,
) -> WaveReport[T]:
    """Runs n coroutines concurrently and measures the batch wall time.

    factory receives the request index — convenient for spreading scenarios
    and idempotency keys. concurrency caps parallelism with a semaphore
    (None — all n start at once).
    """

    async def bounded(semaphore: asyncio.Semaphore, index: int) -> T:
        async with semaphore:
            return await factory(index)

    if concurrency is None:
        coros = [factory(i) for i in range(n)]
    else:
        semaphore = asyncio.Semaphore(concurrency)
        coros = [bounded(semaphore, i) for i in range(n)]
    start = time.perf_counter()
    results = await asyncio.gather(*coros)
    wall_time_s = time.perf_counter() - start
    return WaveReport(label=label or f"n={n}", results=list(results), wall_time_s=wall_time_s)


@dataclass(frozen=True)
class LifecycleResult:
    """One payment's journey through the state machine."""

    payment_id: str | None
    scenario: str
    trajectory: list[str]
    # None — never reached a terminal status (see error).
    final_status: str | None
    error: str | None
    # Callback retries (409/network failure). A non-zero value means commit
    # visibility lag: the service responds before it commits the transaction.
    retries: int = 0


async def poll_until_terminal(
    client: GatewayClient,
    payment_id: str,
    scenario: str = "unknown",
    *,
    timeout_s: float = 30.0,
    interval_s: float = 0.25,
) -> LifecycleResult:
    """Polls GET /payments/{id} until a terminal status or the timeout.

    The trajectory is the statuses we observed: intermediate states between
    polls may be missed, so it is an observation, not a full transition log.
    """
    trajectory: list[str] = []
    last_network_error: str | None = None
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        result = await client.get_payment(payment_id)
        if not result.ok:
            # GET is idempotent, so a network failure is not fatal: under a
            # wave of callbacks the server may drop keep-alive connections —
            # retry until the deadline.
            last_network_error = result.error
            await asyncio.sleep(interval_s)
            continue
        if result.status_code != 200:
            return LifecycleResult(
                payment_id=payment_id,
                scenario=scenario,
                trajectory=trajectory,
                final_status=None,
                error=f"GET /payments: {result.status_code or result.error}",
            )
        status = result.data["status"]
        if not trajectory or trajectory[-1] != status:
            trajectory.append(status)
        if status in TERMINAL_STATUSES:
            return LifecycleResult(
                payment_id=payment_id,
                scenario=scenario,
                trajectory=trajectory,
                final_status=status,
                error=None,
            )
        await asyncio.sleep(interval_s)
    details = f" (last network failure: {last_network_error})" if last_network_error else ""
    return LifecycleResult(
        payment_id=payment_id,
        scenario=scenario,
        trajectory=trajectory,
        final_status=None,
        error=f"no terminal status within {timeout_s}s{details}",
    )


async def _send_step(client: GatewayClient, payment_id: str, step: dict[str, str]) -> RequestResult:
    return await client.send_callback(
        payment_id,
        step["status"],
        failure_reason=step.get("failure_reason"),
        error_message=step.get("error_message"),
    )


async def drive_scenario(
    client: GatewayClient,
    payment_id: str,
    scenario: str,
    *,
    max_retries_per_step: int = 5,
    retry_delay_s: float = 0.1,
) -> LifecycleResult:
    """Drives a payment through a scenario with a chain of callbacks (manual provider mode).

    409s and network failures are retried — like webhook redelivery at a real
    PSP: the service responds to a callback before committing the transaction,
    so the next callback may briefly see the old status. A non-zero retries
    in the result is exactly that visibility lag.
    """
    # A payment is in pending right after successful creation.
    trajectory = ["pending"]
    retries = 0
    for step in SCENARIO_CALLBACKS[scenario]:
        result = await _send_step(client, payment_id, step)
        step_retries = 0
        while (not result.ok or result.status_code == 409) and step_retries < max_retries_per_step:
            step_retries += 1
            await asyncio.sleep(retry_delay_s)
            result = await _send_step(client, payment_id, step)
        retries += step_retries
        if result.status_code != 200:
            detail = result.status_code or result.error
            return LifecycleResult(
                payment_id=payment_id,
                scenario=scenario,
                trajectory=trajectory,
                final_status=None,
                error=f"callback {step['status']}: {detail} {result.data}",
                retries=retries,
            )
        trajectory.append(result.data["status"])
    return LifecycleResult(
        payment_id=payment_id,
        scenario=scenario,
        trajectory=trajectory,
        final_status=trajectory[-1],
        error=None,
        retries=retries,
    )


async def wait_for_status(
    client: GatewayClient, payment_id: str, status: str, *, timeout_s: float = 5.0
) -> bool:
    """Waits until the payment becomes VISIBLE in the given status.

    The service responds before committing the transaction, so right after
    the response the new state may not yet be visible to other requests. This
    barrier is needed before checks sensitive to the payment's current status.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        result = await client.get_payment(payment_id)
        if result.ok and result.status_code == 200 and result.data["status"] == status:
            return True
        await asyncio.sleep(0.05)
    return False


async def wait_for_refunded_amount(
    client: GatewayClient, payment_id: str, expected: str, *, timeout_s: float = 5.0
) -> bool:
    """Waits until the payment's refunded_amount becomes VISIBLE as expected.

    The reservation released by a failed refund is committed after the callback
    response, so the counter lags for a moment — same barrier as
    wait_for_status, only for money instead of a status.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        result = await client.get_payment(payment_id)
        if (
            result.ok
            and result.status_code == 200
            # Compared as Decimal: "6.0" and "6.00" are the same amount.
            and Decimal(str(result.data["refunded_amount"])) == Decimal(expected)
        ):
            return True
        await asyncio.sleep(0.05)
    return False


async def wait_for_refund_status(
    client: GatewayClient, refund_id: str, status: str, *, timeout_s: float = 30.0
) -> bool:
    """Waits until the refund becomes VISIBLE in the given status."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        result = await client.get_refund(refund_id)
        if result.ok and result.status_code == 200 and result.data["status"] == status:
            return True
        await asyncio.sleep(0.1)
    return False


async def run_reconciliation_pass(
    config: GatewayConfig | None = None,
    *,
    stuck_after_seconds: float = 0.0,
    give_up_after_seconds: float = 72000.0,
    batch_size: int = 100,
) -> dict[str, int]:
    """Runs one reconciliation pass in place and returns its report.

    In production this is the separate `reconciler` container with its own
    event loop; here the same use case is called directly so that a notebook
    run can show a pass end to end. The staleness threshold defaults to zero —
    nobody wants to wait fifteen minutes for a refund to count as stuck.

    This is also the only part of the lab that talks to the database instead of
    the API, because the job itself lives behind the API.
    """
    config = config or GatewayConfig()
    # Imported here rather than at module level: everything else in this file
    # is a plain HTTP client and must keep working without the app package.
    sys.path.insert(0, str(_REPO_ROOT))
    from src.contexts.core_payment.application.use_cases.reconcile_stuck_refunds import (
        ReconcileStuckRefundsUseCase,
    )
    from src.contexts.core_payment.infrastructure.database.repositories import (
        SystemSQLAlchemyPaymentRepository,
        SystemSQLAlchemyRefundRepository,
    )
    from src.contexts.core_payment.infrastructure.providers.fake_auto import (
        AutoCallbackFakePaymentProvider,
    )
    from src.shared.database.engine import create_engine, create_sessionmaker

    engine = create_engine(config.database_url)
    maker = create_sessionmaker(engine)
    async with httpx.AsyncClient() as http_client, maker() as session:
        provider = AutoCallbackFakePaymentProvider(
            http_client=http_client,
            callback_url=f"{config.base_url}/callbacks/payments",
            refund_callback_url=f"{config.base_url}/callbacks/refunds",
            callback_secret=config.callback_secret,
            delay_seconds=0.1,
        )
        use_case = ReconcileStuckRefundsUseCase(
            SystemSQLAlchemyRefundRepository(session),
            SystemSQLAlchemyPaymentRepository(session),
            provider,
            session,
            stuck_after=timedelta(seconds=stuck_after_seconds),
            initiation_max_age=timedelta(seconds=give_up_after_seconds),
            batch_size=batch_size,
        )
        report = await use_case()
        await session.commit()
    await engine.dispose()
    return {key: int(value) for key, value in report.model_dump().items()}


async def run_lifecycle(
    client: GatewayClient,
    *,
    mode: ProviderMode,
    scenario: str,
    amount: str = "10.00",
    currency: str = "USD",
    poll_timeout_s: float = 30.0,
) -> LifecycleResult:
    """Full cycle: create a payment and drive it to a terminal status.

    In auto mode the scenario travels to the provider via
    metadata.fake_scenario and the notebook only observes statuses by
    polling. In manual mode the notebook itself sends the callback chain.
    """
    metadata = {"fake_scenario": scenario} if mode == "auto" else None
    created = await client.create_payment(amount=amount, currency=currency, metadata=metadata)
    if created.status_code != 201:
        return LifecycleResult(
            payment_id=None,
            scenario=scenario,
            trajectory=[],
            final_status=None,
            error=f"create: {created.status_code or created.error} {created.data}",
        )
    payment_id = created.payment_id
    assert payment_id is not None  # a 201 without payment_id is a broken API contract
    if mode == "auto":
        return await poll_until_terminal(client, payment_id, scenario, timeout_s=poll_timeout_s)
    return await drive_scenario(client, payment_id, scenario)


@dataclass(frozen=True)
class RefundResult:
    """One refund's journey through its own state machine."""

    payment_id: str
    amount: str
    scenario: str
    refund_id: str | None
    trajectory: list[str]
    final_status: str | None
    error: str | None
    retries: int = 0


@dataclass(frozen=True)
class _RefundProgress:
    """What driving or polling learned about a refund — the part of
    RefundResult that does not depend on how the refund was created."""

    trajectory: list[str]
    final_status: str | None
    error: str | None
    retries: int = 0


async def _poll_refund(
    client: GatewayClient, refund_id: str, *, timeout_s: float, interval_s: float
) -> _RefundProgress:
    trajectory: list[str] = []
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        result = await client.get_refund(refund_id)
        if not result.ok:
            # GET is idempotent: a dropped connection is worth retrying.
            await asyncio.sleep(interval_s)
            continue
        if result.status_code != 200:
            error = f"GET /refunds: {result.status_code or result.error}"
            return _RefundProgress(trajectory, None, error)
        status = result.data["status"]
        if not trajectory or trajectory[-1] != status:
            trajectory.append(status)
        if status in TERMINAL_STATUSES:
            return _RefundProgress(trajectory, status, None)
        await asyncio.sleep(interval_s)
    return _RefundProgress(trajectory, None, f"no terminal status within {timeout_s}s")


async def _send_refund_step(
    client: GatewayClient, refund_id: str, step: dict[str, str]
) -> RequestResult:
    return await client.send_refund_callback(
        refund_id,
        step["status"],
        failure_reason=step.get("failure_reason"),
        error_message=step.get("error_message"),
    )


async def _drive_refund(
    client: GatewayClient,
    refund_id: str,
    scenario: str,
    *,
    max_retries_per_step: int = 5,
    retry_delay_s: float = 0.1,
) -> _RefundProgress:
    # A successful creation leaves the refund in pending.
    trajectory = ["pending"]
    retries = 0
    for step in REFUND_SCENARIO_CALLBACKS[scenario]:
        result = await _send_refund_step(client, refund_id, step)
        step_retries = 0
        # A 409 here means the refund is still visible as created: the service
        # commits after answering, so a callback can outrun its own transaction.
        while (not result.ok or result.status_code == 409) and step_retries < max_retries_per_step:
            step_retries += 1
            await asyncio.sleep(retry_delay_s)
            result = await _send_refund_step(client, refund_id, step)
        retries += step_retries
        if result.status_code != 200:
            detail = result.status_code or result.error
            error = f"callback {step['status']}: {detail} {result.data}"
            return _RefundProgress(trajectory, None, error, retries)
        trajectory.append(result.data["status"])
    return _RefundProgress(trajectory, trajectory[-1], None, retries)


async def run_refund(
    client: GatewayClient,
    payment_id: str,
    *,
    mode: ProviderMode,
    scenario: str = "success",
    amount: str = "10.00",
    idempotency_key: str | None = None,
    poll_timeout_s: float = 30.0,
) -> RefundResult:
    """Creates a refund on an already successful payment and drives it to a terminal status.

    Mirrors run_lifecycle: in auto mode the scenario travels to the provider in
    metadata.fake_scenario and we only observe, in manual mode the notebook
    sends the callback itself.
    """
    metadata = {"fake_scenario": scenario} if mode == "auto" else None
    created = await client.create_refund(
        payment_id, amount=amount, metadata=metadata, idempotency_key=idempotency_key
    )
    if created.status_code != 201:
        return RefundResult(
            payment_id=payment_id,
            amount=amount,
            scenario=scenario,
            refund_id=created.refund_id,
            trajectory=[],
            final_status=None,
            error=f"create: {created.status_code or created.error} {created.data}",
        )
    refund_id = created.refund_id
    assert refund_id is not None  # a 201 without refund_id is a broken API contract
    if mode == "auto":
        progress = await _poll_refund(client, refund_id, timeout_s=poll_timeout_s, interval_s=0.25)
    else:
        progress = await _drive_refund(client, refund_id, scenario)
    return RefundResult(
        payment_id=payment_id,
        amount=amount,
        scenario=scenario,
        refund_id=refund_id,
        trajectory=progress.trajectory,
        final_status=progress.final_status,
        error=progress.error,
        retries=progress.retries,
    )


def _quantile_ms(latencies: pd.Series[float], q: float) -> float | None:
    return None if latencies.empty else round(float(latencies.quantile(q)), 1)


def summarize_waves(reports: Sequence[WaveReport[RequestResult]]) -> pd.DataFrame:
    """Per-wave summary: response codes, latency percentiles, RPS."""
    rows = []
    for report in reports:
        latencies = pd.Series([r.elapsed_ms for r in report.results if r.ok], dtype=float)
        codes = Counter(r.status_code for r in report.results if r.ok)
        rows.append(
            {
                "wave": report.label,
                "requests": len(report.results),
                "network_errors": sum(1 for r in report.results if not r.ok),
                "http_codes": ", ".join(f"{code}:{count}" for code, count in sorted(codes.items())),
                "p50_ms": _quantile_ms(latencies, 0.50),
                "p95_ms": _quantile_ms(latencies, 0.95),
                "p99_ms": _quantile_ms(latencies, 0.99),
                "wall_s": round(report.wall_time_s, 2),
                "rps": round(len(report.results) / report.wall_time_s, 1),
            }
        )
    return pd.DataFrame(rows)


def summarize_refunds(results: Sequence[RefundResult]) -> pd.DataFrame:
    """One row per refund: did each reach its expected terminal status."""
    rows = []
    for result in results:
        expected = REFUND_SCENARIO_EXPECTED_FINAL.get(result.scenario)
        rows.append(
            {
                "scenario": result.scenario,
                "amount": result.amount,
                "refund_id": result.refund_id,
                "trajectory": " -> ".join(result.trajectory),
                "final_status": result.final_status,
                "expected": expected,
                "ok": result.final_status is not None and result.final_status == expected,
                "retries": result.retries,
                "error": result.error,
            }
        )
    return pd.DataFrame(rows)


def summarize_lifecycles(results: Sequence[LifecycleResult]) -> pd.DataFrame:
    """One row per payment: did each reach its expected terminal status."""
    rows = []
    for result in results:
        expected = SCENARIO_EXPECTED_FINAL.get(result.scenario)
        rows.append(
            {
                "scenario": result.scenario,
                "payment_id": result.payment_id,
                "trajectory": " -> ".join(result.trajectory),
                "final_status": result.final_status,
                "expected": expected,
                "ok": result.final_status is not None and result.final_status == expected,
                "retries": result.retries,
                "error": result.error,
            }
        )
    return pd.DataFrame(rows)
