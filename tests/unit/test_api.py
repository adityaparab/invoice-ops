"""Offline HTTP contract and request lifecycle checks for the FastAPI shell."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated

import httpx
import pytest
from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel

from invoiceops_agent.api.app import create_app
from invoiceops_agent.api.context import RequestContext, get_request_context
from invoiceops_agent.api.dependencies import (
    DependencyFactory,
    DependencyUnavailable,
    ReadinessChecks,
)
from invoiceops_agent.api.schemas.problem import ProblemDetails
from invoiceops_agent.api.settings import ApiSettings

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]
TRACE_ID = "0123456789abcdef0123456789abcdef"


@dataclass
class Probe:
    calls: int = 0
    error: Exception | None = None

    async def __call__(self) -> None:
        self.calls += 1
        if self.error is not None:
            raise self.error


def make_factory(checks: ReadinessChecks, events: list[str]) -> DependencyFactory:
    @asynccontextmanager
    async def factory(settings: ApiSettings) -> AsyncIterator[ReadinessChecks]:
        events.append("opened")
        try:
            yield checks
        finally:
            events.append("closed")

    return factory


@asynccontextmanager
async def client_for(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as client:
            yield client


def assert_problem(response: httpx.Response, status: int) -> ProblemDetails:
    assert response.status_code == status
    assert response.headers["content-type"] == "application/problem+json"
    problem = ProblemDetails.model_validate(response.json())
    assert problem.status == status
    assert problem.trace_id == response.headers["x-trace-id"]
    assert problem.type == "about:blank"
    return problem


async def test_liveness_never_probes_and_lifespan_closes_dependencies() -> None:
    postgres, minio = Probe(), Probe()
    events: list[str] = []
    app = create_app(dependency_factory=make_factory(ReadinessChecks(postgres, minio), events))
    assert events == []
    async with client_for(app) as client:
        assert events == ["opened"]
        response = await client.get("/healthz", headers={"X-Trace-ID": TRACE_ID})
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
        assert response.headers["x-trace-id"] == TRACE_ID
        assert postgres.calls == minio.calls == 0
    assert events == ["opened", "closed"]


async def test_default_lifespan_works_without_config_or_network() -> None:
    async with client_for(create_app()) as client:
        assert (await client.get("/healthz")).status_code == 200
        response = await client.get("/readyz")
        problem = assert_problem(response, 503)
        assert problem.dependencies is not None
        assert problem.dependencies.model_dump() == {
            "postgres": "unconfigured",
            "minio": "unconfigured",
        }


async def test_readiness_requires_lifespan() -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()), base_url="http://test"
    ) as client:
        problem = assert_problem(await client.get("/readyz"), 503)
        assert problem.dependencies is not None
        assert problem.dependencies.postgres == "unavailable"


async def test_readiness_probes_both_dependencies_and_returns_ready() -> None:
    postgres, minio = Probe(), Probe()
    app = create_app(dependency_factory=make_factory(ReadinessChecks(postgres, minio), []))
    async with client_for(app) as client:
        response = await client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "dependencies": {"postgres": "ok", "minio": "ok"},
    }
    assert postgres.calls == minio.calls == 1


@pytest.mark.parametrize("failed_dependency", ["postgres", "minio"])
async def test_readiness_reports_partial_outages(failed_dependency: str) -> None:
    postgres, minio = Probe(), Probe()
    failed = postgres if failed_dependency == "postgres" else minio
    failed.error = DependencyUnavailable("private credentials must never appear")
    app = create_app(dependency_factory=make_factory(ReadinessChecks(postgres, minio), []))
    async with client_for(app) as client:
        response = await client.get("/readyz")
    problem = assert_problem(response, 503)
    assert problem.dependencies is not None
    assert problem.dependencies.model_dump()[failed_dependency] == "unavailable"
    assert "private credentials" not in response.text
    assert postgres.calls == minio.calls == 1


async def test_readiness_cancels_a_stuck_probe() -> None:
    cancelled = asyncio.Event()

    async def stuck() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    app = create_app(
        ApiSettings(readiness_timeout_seconds=0.01),
        dependency_factory=make_factory(ReadinessChecks(stuck, Probe()), []),
    )
    async with client_for(app) as client:
        problem = assert_problem(await client.get("/readyz"), 503)
    assert problem.dependencies is not None
    assert problem.dependencies.postgres == "timeout"
    assert problem.dependencies.minio == "ok"
    assert cancelled.is_set()


async def test_dependency_probes_run_concurrently() -> None:
    both_started = asyncio.Event()
    started = 0

    async def barrier() -> None:
        nonlocal started
        started += 1
        if started == 2:
            both_started.set()
        await both_started.wait()

    app = create_app(dependency_factory=make_factory(ReadinessChecks(barrier, barrier), []))
    async with client_for(app) as client:
        assert (await client.get("/readyz")).status_code == 200
    assert started == 2


async def test_unexpected_adapter_error_is_sanitized_and_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = create_app(
        dependency_factory=make_factory(
            ReadinessChecks(Probe(error=RuntimeError("secret-value")), Probe()), []
        )
    )
    async with client_for(app) as client:
        response = await client.get("/readyz", headers={"X-Trace-ID": TRACE_ID})
    assert_problem(response, 503)
    assert "secret-value" not in response.text + caplog.text
    assert f"trace_id={TRACE_ID}" in caplog.text
    assert "error_type=RuntimeError" in caplog.text
    assert "duration_ms=" in caplog.text


async def test_lifespan_closes_dependencies_after_failure() -> None:
    events: list[str] = []
    app = create_app(dependency_factory=make_factory(ReadinessChecks(Probe(), Probe()), events))
    with pytest.raises(RuntimeError, match="simulated shutdown"):
        async with app.router.lifespan_context(app):
            raise RuntimeError("simulated shutdown")
    assert events == ["opened", "closed"]


@pytest.mark.parametrize("trace_id", [None, "not-a-trace", "0" * 32, "A" * 32, "a" * 33])
async def test_invalid_or_absent_trace_ids_are_replaced(trace_id: str | None) -> None:
    async with client_for(create_app()) as client:
        response = await client.get(
            "/healthz", headers={"X-Trace-ID": trace_id} if trace_id else {}
        )
    new_id = response.headers["x-trace-id"]
    assert len(new_id) == 32
    assert int(new_id, 16) > 0
    assert new_id != trace_id


async def test_duplicate_trace_ids_are_replaced() -> None:
    async with client_for(create_app()) as client:
        response = await client.get(
            "/healthz", headers=[("X-Trace-ID", TRACE_ID), ("X-Trace-ID", "f" * 32)]
        )
    assert response.headers["x-trace-id"] not in {TRACE_ID, "f" * 32}


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize("key", [None, "", "has spaces", "!not-valid", "k" * 129])
async def test_mutations_require_a_valid_key_before_routing(method: str, key: str | None) -> None:
    async with client_for(create_app()) as client:
        response = await client.request(
            method, "/not-a-route", headers={"Idempotency-Key": key} if key is not None else {}
        )
    assert_problem(response, 400)


async def test_duplicate_idempotency_headers_are_rejected() -> None:
    async with client_for(create_app()) as client:
        response = await client.post(
            "/not-a-route", headers=[("Idempotency-Key", "one"), ("Idempotency-Key", "two")]
        )
    assert_problem(response, 400)


@pytest.mark.parametrize("key", ["a", "a" * 128, "invoice:2026-01_a.pdf"])
async def test_validated_request_context_is_injectable(key: str) -> None:
    app = create_app()

    @app.post("/test/mutation")
    async def mutation(
        context: Annotated[RequestContext, Depends(get_request_context)],
    ) -> dict[str, str | None]:
        return {"key": context.idempotency_key, "trace_id": context.trace_id}

    async with client_for(app) as client:
        response = await client.post(
            "/test/mutation", headers={"Idempotency-Key": key, "X-Trace-ID": TRACE_ID}
        )
    assert response.status_code == 200
    assert response.json() == {"key": key, "trace_id": TRACE_ID}


@pytest.mark.parametrize("path,status", [("/unknown", 404), ("/healthz/extra", 404)])
async def test_router_errors_use_problem_details(path: str, status: int) -> None:
    async with client_for(create_app()) as client:
        problem = assert_problem(await client.get(path), status)
    assert problem.instance == path


async def test_method_not_allowed_preserves_allow_header() -> None:
    async with client_for(create_app()) as client:
        response = await client.post("/healthz", headers={"Idempotency-Key": "attempt-1"})
    assert_problem(response, 405)
    assert response.headers["allow"] == "GET"


async def test_http_exceptions_preserve_auth_headers() -> None:
    app = create_app()

    @app.get("/test/private")
    async def private() -> None:
        raise HTTPException(
            401, detail="Authentication required", headers={"WWW-Authenticate": "Bearer"}
        )

    async with client_for(app) as client:
        response = await client.get("/test/private")
    problem = assert_problem(response, 401)
    assert problem.detail == "Authentication required"
    assert response.headers["www-authenticate"] == "Bearer"


async def test_validation_errors_do_not_echo_request_secrets() -> None:
    app = create_app()

    class Payload(BaseModel):
        count: int

    @app.post("/test/validation")
    async def validate(payload: Payload) -> Payload:
        return payload

    async with client_for(app) as client:
        response = await client.post(
            "/test/validation",
            json={"count": "secret-value"},
            headers={"Idempotency-Key": "test-validation"},
        )
    assert_problem(response, 422)
    assert "secret-value" not in response.text


async def test_unexpected_errors_have_trace_and_no_internal_detail(
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = create_app()

    @app.get("/test/broken")
    async def broken() -> None:
        raise RuntimeError("secret-value")

    async with client_for(app) as client:
        response = await client.get("/test/broken", headers={"X-Trace-ID": TRACE_ID})
    problem = assert_problem(response, 500)
    assert problem.trace_id == TRACE_ID
    assert "secret-value" not in response.text + caplog.text
    assert f"trace_id={TRACE_ID}" in caplog.text


async def test_response_validation_failures_use_sanitized_problem_details() -> None:
    from invoiceops_agent.api.schemas.health import LivenessResponse

    app = create_app()

    @app.get("/test/invalid-response", response_model=LivenessResponse)
    async def invalid_response() -> dict[str, str]:
        return {"status": "private-internal-value"}

    async with client_for(app) as client:
        response = await client.get("/test/invalid-response")
    assert_problem(response, 500)
    assert "private-internal-value" not in response.text


async def test_cancelling_readiness_cleans_up_all_probes() -> None:
    from invoiceops_agent.api.dependencies import check_readiness

    both_started = asyncio.Event()
    started = 0
    cleaned_up = 0

    async def blocked() -> None:
        nonlocal started, cleaned_up
        started += 1
        if started == 2:
            both_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned_up += 1

    task = asyncio.create_task(
        check_readiness(ReadinessChecks(blocked, blocked), timeout_seconds=2, trace_id=TRACE_ID)
    )
    await both_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleaned_up == 2
