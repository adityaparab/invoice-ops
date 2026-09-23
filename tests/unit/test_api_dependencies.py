"""Async readiness adapters use only fake transports and fake database connections."""

from types import TracebackType

import httpx
import psycopg
import pytest
from pydantic import SecretStr, ValidationError

from invoiceops_agent.api.dependencies import (
    DependencyUnavailable,
    DependencyUnconfigured,
    MinioReadiness,
    PostgresReadiness,
    default_dependency_factory,
)
from invoiceops_agent.api.settings import ApiSettings

pytestmark = pytest.mark.unit


def test_settings_load_environment_and_redact_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INVOICEOPS_POSTGRES_DSN", "postgresql://user:secret-value@postgres/db")
    monkeypatch.setenv("INVOICEOPS_MINIO_URL", "http://minio:9000")
    monkeypatch.setenv("INVOICEOPS_READINESS_TIMEOUT_SECONDS", "0.5")
    settings = ApiSettings()
    assert settings.postgres_dsn is not None
    assert settings.postgres_dsn.get_secret_value() == "postgresql://user:secret-value@postgres/db"
    assert str(settings.minio_url) == "http://minio:9000/"
    assert settings.readiness_timeout_seconds == 0.5
    assert "secret-value" not in repr(settings)


@pytest.mark.parametrize("value", [0, -1, 31, float("nan"), float("inf")])
def test_readiness_timeout_must_be_bounded(value: float) -> None:
    with pytest.raises(ValidationError):
        ApiSettings(readiness_timeout_seconds=value)


@pytest.mark.parametrize(
    "url", ["ftp://minio", "http://user:password@minio", "http://minio/path", "http://minio?q=x"]
)
def test_minio_endpoint_must_be_an_http_origin(url: str) -> None:
    with pytest.raises(ValidationError):
        ApiSettings.model_validate({"minio_url": url})


def test_postgres_dsn_requires_postgres_scheme() -> None:
    with pytest.raises(ValidationError):
        ApiSettings(postgres_dsn=SecretStr("mysql://example/db"))


@pytest.mark.asyncio
async def test_default_factory_closes_client_without_initial_network_calls() -> None:
    async with default_dependency_factory(ApiSettings()) as checks:
        assert isinstance(checks.minio, MinioReadiness)
        client = checks.minio.client
        assert not client.is_closed
    assert client.is_closed


@pytest.mark.asyncio
async def test_unconfigured_adapters_fail_without_network_calls() -> None:
    async with default_dependency_factory(ApiSettings()) as checks:
        with pytest.raises(DependencyUnconfigured):
            await checks.postgres()
        with pytest.raises(DependencyUnconfigured):
            await checks.minio()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 503, 307])
async def test_minio_uses_bounded_ready_probe_and_rejects_redirects(status: int) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status, headers={"Location": "http://elsewhere/"})

    settings = ApiSettings.model_validate({"minio_url": "http://minio:9000"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        check = MinioReadiness(settings, client)
        if status == 200:
            await check()
        else:
            with pytest.raises(DependencyUnavailable):
                await check()
    assert len(requests) == 1
    assert str(requests[0].url) == "http://minio:9000/minio/health/ready"


@pytest.mark.asyncio
@pytest.mark.parametrize("timed_out", [False, True])
async def test_minio_transport_failures_have_typed_errors(timed_out: bool) -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        if timed_out:
            raise httpx.ReadTimeout("private endpoint", request=request)
        raise httpx.ConnectError("private endpoint", request=request)

    settings = ApiSettings.model_validate({"minio_url": "http://minio:9000"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as client:
        with pytest.raises(TimeoutError if timed_out else DependencyUnavailable):
            await MinioReadiness(settings, client)()


class FakeConnection:
    def __init__(self) -> None:
        self.executed: list[str] = []
        self.closed = False

    async def execute(self, query: str) -> None:
        self.executed.append(query)

    async def __aenter__(self) -> "FakeConnection":
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_postgres_probes_and_closes_the_async_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection()
    calls: list[tuple[str, bool]] = []

    async def connect(dsn: str, *, autocommit: bool) -> FakeConnection:
        calls.append((dsn, autocommit))
        return connection

    monkeypatch.setattr(psycopg.AsyncConnection, "connect", connect)
    await PostgresReadiness(ApiSettings(postgres_dsn=SecretStr("postgresql://user@postgres/db")))()
    assert calls == [("postgresql://user@postgres/db", True)]
    assert connection.executed == ["SELECT 1"]
    assert connection.closed


@pytest.mark.asyncio
async def test_postgres_connect_errors_are_typed(monkeypatch: pytest.MonkeyPatch) -> None:
    async def connect(dsn: str, *, autocommit: bool) -> FakeConnection:
        raise psycopg.OperationalError("private connection details")

    monkeypatch.setattr(psycopg.AsyncConnection, "connect", connect)
    with pytest.raises(DependencyUnavailable, match="Postgres readiness failed"):
        await PostgresReadiness(
            ApiSettings(postgres_dsn=SecretStr("postgresql://user@postgres/db"))
        )()


def test_invalid_settings_diagnostics_omit_raw_env_input(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INVOICEOPS_POSTGRES_DSN", "mysql://user:secret-value@database/db")
    with pytest.raises(ValidationError) as error:
        ApiSettings()
    assert "secret-value" not in str(error.value)


@pytest.mark.asyncio
async def test_postgres_query_failure_still_closes_the_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailedConnection(FakeConnection):
        async def execute(self, query: str) -> None:
            raise psycopg.OperationalError("private-query-details")

    connection = FailedConnection()

    async def connect(dsn: str, *, autocommit: bool) -> FailedConnection:
        return connection

    monkeypatch.setattr(psycopg.AsyncConnection, "connect", connect)
    with pytest.raises(DependencyUnavailable):
        await PostgresReadiness(
            ApiSettings(postgres_dsn=SecretStr("postgresql://user@postgres/db"))
        )()
    assert connection.closed
