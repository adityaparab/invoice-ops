"""Bounded async infrastructure probes and their injectable lifecycle boundary."""

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from time import perf_counter
from typing import Protocol

import httpx
import psycopg

from invoiceops_agent.api.schemas.health import DependencyStatus, DependencyStatuses
from invoiceops_agent.api.settings import ApiSettings

logger = logging.getLogger(__name__)


class DependencyError(Exception):
    """A dependency could not satisfy its readiness check."""


class DependencyUnconfigured(DependencyError):
    """The deployment has not supplied this dependency's configuration."""


class DependencyUnavailable(DependencyError):
    """An infrastructure probe failed without exposing connection details."""


class ReadinessCheck(Protocol):
    async def __call__(self) -> None:
        """Raise a typed dependency error when the dependency is not ready."""
        ...


@dataclass(frozen=True)
class ReadinessChecks:
    postgres: ReadinessCheck
    minio: ReadinessCheck


DependencyFactory = Callable[[ApiSettings], AbstractAsyncContextManager[ReadinessChecks]]


@dataclass
class ApiRuntime:
    checks: ReadinessChecks | None = None


@dataclass(frozen=True)
class PostgresReadiness:
    settings: ApiSettings

    async def __call__(self) -> None:
        if self.settings.postgres_dsn is None:
            raise DependencyUnconfigured("Postgres is not configured")
        try:
            async with await psycopg.AsyncConnection.connect(
                self.settings.postgres_dsn.get_secret_value(), autocommit=True
            ) as connection:
                await connection.execute("SELECT 1")
        except psycopg.Error as error:
            raise DependencyUnavailable("Postgres readiness failed") from error


@dataclass(frozen=True)
class MinioReadiness:
    settings: ApiSettings
    client: httpx.AsyncClient

    async def __call__(self) -> None:
        if self.settings.minio_url is None:
            raise DependencyUnconfigured("MinIO is not configured")
        try:
            response = await self.client.get(f"{self.settings.minio_url}minio/health/ready")
            response.raise_for_status()
        except httpx.TimeoutException as error:
            raise TimeoutError("MinIO readiness timed out") from error
        except httpx.HTTPError as error:
            raise DependencyUnavailable("MinIO readiness failed") from error


@asynccontextmanager
async def default_dependency_factory(settings: ApiSettings) -> AsyncIterator[ReadinessChecks]:
    """Allocate the HTTP client in lifespan; no probe runs until readiness is requested."""
    async with httpx.AsyncClient(
        timeout=settings.readiness_timeout_seconds, follow_redirects=False, trust_env=False
    ) as client:
        yield ReadinessChecks(PostgresReadiness(settings), MinioReadiness(settings, client))


async def _probe(
    name: str, check: ReadinessCheck, timeout_seconds: float, trace_id: str
) -> DependencyStatus:
    started = perf_counter()
    status: DependencyStatus = "ok"
    try:
        async with asyncio.timeout(timeout_seconds):
            await check()
    except DependencyUnconfigured:
        status = "unconfigured"
    except TimeoutError:
        status = "timeout"
    except DependencyError:
        status = "unavailable"
    except Exception as error:
        # A failed custom adapter must degrade readiness without leaking credentials in its message.
        logger.error(
            "readiness_adapter_failed dependency=%s trace_id=%s error_type=%s",
            name,
            trace_id,
            type(error).__name__,
        )
        status = "unavailable"
    logger.log(
        logging.INFO if status == "ok" else logging.WARNING,
        "readiness_checked dependency=%s status=%s trace_id=%s duration_ms=%.3f",
        name,
        status,
        trace_id,
        (perf_counter() - started) * 1000,
    )
    return status


async def check_readiness(
    checks: ReadinessChecks, *, timeout_seconds: float, trace_id: str
) -> DependencyStatuses:
    """Check required infrastructure concurrently within independent request-time bounds."""
    postgres, minio = await asyncio.gather(
        _probe("postgres", checks.postgres, timeout_seconds, trace_id),
        _probe("minio", checks.minio, timeout_seconds, trace_id),
    )
    return DependencyStatuses(postgres=postgres, minio=minio)
