"""Low-cardinality OpenTelemetry metrics for API requests and gateway calls."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import ROUND_HALF_UP, Decimal

from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.prometheus import PrometheusMetricReader
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import MetricReader, PeriodicExportingMetricReader
from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from prometheus_client import CollectorRegistry, generate_latest
from pydantic import AnyHttpUrl, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from invoiceops_agent.obs.tracing import _headers


class MetricSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="OTEL_",
        env_file=".env",
        env_ignore_empty=True,
        extra="ignore",
        hide_input_in_errors=True,
    )

    exporter_otlp_metrics_endpoint: AnyHttpUrl | None = None
    exporter_otlp_metrics_headers: SecretStr | None = None


def usd_to_nano_usd(amount: Decimal) -> int:
    return int((amount * Decimal(1_000_000_000)).to_integral_value(rounding=ROUND_HALF_UP))


class Metrics:
    def __init__(self, provider: MeterProvider, registry: CollectorRegistry | None) -> None:
        self._provider = provider
        self._registry = registry
        meter = provider.get_meter("invoiceops.metrics")
        self._requests = meter.create_counter("invoiceops.api.requests", unit="{request}")
        self._request_duration = meter.create_histogram("invoiceops.api.duration", unit="s")
        self._gateway_calls = meter.create_counter("invoiceops.gateway.calls", unit="{call}")
        self._gateway_duration = meter.create_histogram("invoiceops.gateway.duration", unit="s")
        self._gateway_cost = meter.create_counter("invoiceops.gateway.cost", unit="nUSD")
        self._input_tokens = meter.create_counter("invoiceops.gateway.input_tokens", unit="{token}")
        self._output_tokens = meter.create_counter(
            "invoiceops.gateway.output_tokens", unit="{token}"
        )

    def record_request(self, method: str, status: int, duration_seconds: float) -> None:
        attributes = {
            "http.request.method": method,
            "http.response.status_class": f"{status // 100}xx",
        }
        self._requests.add(1, attributes)
        self._request_duration.record(duration_seconds, attributes)

    def record_gateway(
        self,
        *,
        alias: str,
        status: str,
        latency_ms: float,
        cost_usd: Decimal | None,
        input_tokens: int | None,
        output_tokens: int | None,
    ) -> None:
        attributes = {"alias": alias, "status": status}
        self._gateway_calls.add(1, attributes)
        self._gateway_duration.record(latency_ms / 1000, attributes)
        if cost_usd is not None:
            self._gateway_cost.add(usd_to_nano_usd(cost_usd), attributes)
        if input_tokens is not None:
            self._input_tokens.add(input_tokens, attributes)
        if output_tokens is not None:
            self._output_tokens.add(output_tokens, attributes)

    def render(self) -> bytes:
        if self._registry is None:
            raise RuntimeError("Prometheus reader is not configured")
        return generate_latest(self._registry)

    @property
    def registry(self) -> CollectorRegistry:
        if self._registry is None:
            raise RuntimeError("Prometheus reader is not configured")
        return self._registry

    async def shutdown(self) -> None:
        await asyncio.to_thread(self._provider.shutdown)


@asynccontextmanager
async def metrics_session(service_name: str, *, prometheus: bool = False) -> AsyncIterator[Metrics]:
    settings = MetricSettings()
    registry = CollectorRegistry(auto_describe=True) if prometheus else None
    readers: list[MetricReader] = (
        [PrometheusMetricReader(registry=registry)] if registry is not None else []
    )
    if settings.exporter_otlp_metrics_endpoint is not None:
        readers.append(
            PeriodicExportingMetricReader(
                OTLPMetricExporter(
                    endpoint=str(settings.exporter_otlp_metrics_endpoint),
                    headers=_headers(settings.exporter_otlp_metrics_headers),
                ),
                export_interval_millis=15_000 if prometheus else 86_400_000,
            )
        )
    provider = MeterProvider(
        resource=Resource.create({SERVICE_NAME: service_name}),
        metric_readers=readers,
        views=[
            View(
                instrument_name=instrument,
                aggregation=ExplicitBucketHistogramAggregation(
                    [0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120]
                ),
            )
            for instrument in ("invoiceops.api.duration", "invoiceops.gateway.duration")
        ],
    )
    metrics = Metrics(provider, registry)
    try:
        yield metrics
    finally:
        await metrics.shutdown()
