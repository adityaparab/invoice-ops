"""Sanitized workflow spans and opt-in OTLP export."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from typing import Protocol
from uuid import UUID

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Status, StatusCode
from pydantic import AnyHttpUrl, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class TraceIdentity(Protocol):
    @property
    def run_id(self) -> UUID: ...

    @property
    def invoice_id(self) -> UUID: ...

    @property
    def trace_id(self) -> str: ...


class TraceSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="OTEL_",
        env_file=".env",
        env_ignore_empty=True,
        extra="ignore",
        hide_input_in_errors=True,
    )

    exporter_otlp_traces_endpoint: AnyHttpUrl | None = None
    exporter_otlp_traces_headers: SecretStr | None = None


def _headers(value: SecretStr | None) -> dict[str, str]:
    if value is None:
        return {}
    headers: dict[str, str] = {}
    for item in value.get_secret_value().split(","):
        key, separator, header_value = item.partition("=")
        if not separator or not key.strip() or not header_value.strip():
            raise ValueError("OTLP trace headers must be comma-separated key=value pairs")
        headers[key.strip()] = header_value.strip()
    return headers


def build_tracer_provider(settings: TraceSettings, *, service_name: str) -> TracerProvider | None:
    """Build a provider only when an OTLP destination is explicitly configured."""
    if settings.exporter_otlp_traces_endpoint is None:
        return None
    headers = _headers(settings.exporter_otlp_traces_headers)
    provider = TracerProvider(resource=Resource.create({SERVICE_NAME: service_name}))
    provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(
                endpoint=str(settings.exporter_otlp_traces_endpoint),
                headers=headers,
            )
        )
    )
    return provider


@asynccontextmanager
async def tracing_session(service_name: str) -> AsyncIterator[None]:
    """Initialize export for a process and flush without blocking its event loop."""
    provider = build_tracer_provider(TraceSettings(), service_name=service_name)
    if provider is None:
        yield
        return
    trace.set_tracer_provider(provider)
    try:
        yield
    finally:
        await asyncio.to_thread(provider.shutdown)


@contextmanager
def operation_span(kind: str, name: str, identity: TraceIdentity) -> Iterator[None]:
    """Record only stable identifiers and error types; never invoice or prompt content."""
    tracer = trace.get_tracer("invoiceops.workflow")
    with tracer.start_as_current_span(
        f"invoiceops.{kind}.{name}", record_exception=False, set_status_on_exception=False
    ) as span:
        span.set_attribute("invoiceops.run_id", str(identity.run_id))
        span.set_attribute("invoiceops.invoice_id", str(identity.invoice_id))
        span.set_attribute("invoiceops.trace_id", identity.trace_id)
        span.set_attribute("invoiceops.operation.kind", kind)
        try:
            yield
        except asyncio.CancelledError:
            span.set_attribute("error.type", "CancelledError")
            span.set_status(Status(StatusCode.ERROR))
            raise
        except Exception as error:
            span.set_attribute("error.type", type(error).__name__)
            span.set_status(Status(StatusCode.ERROR))
            raise


async def traced_call[Result](
    kind: str, name: str, identity: TraceIdentity, operation: Callable[[], Awaitable[Result]]
) -> Result:
    with operation_span(kind, name, identity):
        return await operation()


class TracedNode[Identity: TraceIdentity, Result]:
    def __init__(self, name: str, operation: Callable[[Identity], Awaitable[Result]]) -> None:
        self._name = name
        self._operation = operation

    async def __call__(self, state: Identity) -> Result:
        return await traced_call("node", self._name, state, lambda: self._operation(state))


def traced_node[Identity: TraceIdentity, Result](
    name: str, operation: Callable[[Identity], Awaitable[Result]]
) -> TracedNode[Identity, Result]:
    return TracedNode(name, operation)
