"""Sanitized gateway outcomes and Langfuse-compatible OpenTelemetry spans."""

import asyncio
import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from typing import Literal, Protocol

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from invoiceops_agent.gateway_client.schemas import ModelAlias, RequestContext, TokenUsage, Version
from invoiceops_agent.obs.metrics import Metrics

logger = logging.getLogger(__name__)


class GatewayEvent(RequestContext):
    alias: ModelAlias
    requested_model: Version
    model: Version | None = None
    model_version: Version
    status: Literal["succeeded", "failed", "cancelled"]
    attempts: int
    latency_ms: float
    error_code: str | None = None
    usage: TokenUsage | None = None
    cost_usd: Decimal | None = None
    route_index: int = 0
    observed_run_cost_usd: Decimal | None = None
    budget_alert: bool = False
    cache_hit: bool = False


class GatewayTelemetry(Protocol):
    def record(self, event: GatewayEvent) -> None:
        """Record metadata only; implementations must not throw or block on I/O."""


class LoggingTelemetry:
    def record(self, event: GatewayEvent) -> None:
        logger.info(
            "event=gateway.call run_id=%s trace_id=%s alias=%s requested_model=%s "
            "model=%s model_version=%s "
            "prompt_version=%s status=%s attempts=%d route_index=%d latency_ms=%.1f "
            "input_tokens=%s output_tokens=%s cost_usd=%s "
            "observed_run_cost_usd=%s budget_alert=%s cache_hit=%s error_code=%s",
            event.run_id,
            event.trace_id,
            event.alias,
            event.requested_model,
            event.model,
            event.model_version,
            event.prompt_version,
            event.status,
            event.attempts,
            event.route_index,
            event.latency_ms,
            event.usage.input_tokens if event.usage else None,
            event.usage.output_tokens if event.usage else None,
            event.cost_usd,
            event.observed_run_cost_usd,
            event.budget_alert,
            event.cache_hit,
            event.error_code,
        )
        if event.budget_alert:
            logger.warning(
                "event=gateway.budget_alert run_id=%s alias=%s observed_run_cost_usd=%s",
                event.run_id,
                event.alias,
                event.observed_run_cost_usd,
            )


class MetricGatewayTelemetry:
    """Record logical gateway calls once while preserving structured call logs."""

    def __init__(self, metrics: Metrics) -> None:
        self._metrics = metrics
        self._logging = LoggingTelemetry()

    def record(self, event: GatewayEvent) -> None:
        if event.cache_hit:
            self._metrics.record_cache_hit(event.alias)
        else:
            self._metrics.record_gateway(
                alias=event.alias,
                status=event.status,
                latency_ms=event.latency_ms,
                cost_usd=event.cost_usd,
                input_tokens=event.usage.input_tokens if event.usage else None,
                output_tokens=event.usage.output_tokens if event.usage else None,
            )
        if event.budget_alert:
            self._metrics.record_budget_alert(event.alias)
        self._logging.record(event)


@contextmanager
def gateway_span(context: RequestContext, alias: ModelAlias) -> Iterator[None]:
    """Keep one generation or embedding span open across retries and validation."""
    tracer = trace.get_tracer("invoiceops.gateway")
    with tracer.start_as_current_span(
        f"invoiceops.llm.{alias}", record_exception=False, set_status_on_exception=False
    ) as span:
        span.set_attribute(
            "langfuse.observation.type", "embedding" if alias == "embed" else "generation"
        )
        span.set_attribute("gen_ai.operation.name", "embeddings" if alias == "embed" else "chat")
        span.set_attribute("langfuse.observation.metadata.run_id", str(context.run_id))
        span.set_attribute("langfuse.observation.metadata.trace_id", context.trace_id)
        span.set_attribute("langfuse.observation.metadata.prompt_version", context.prompt_version)
        span.set_attribute("langfuse.observation.metadata.scenario", context.scenario)
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


class SpanGatewayTelemetry:
    """Annotate the active call span while preserving injected telemetry sinks."""

    def __init__(self, delegate: GatewayTelemetry) -> None:
        self._delegate = delegate

    def record(self, event: GatewayEvent) -> None:
        span = trace.get_current_span()
        if span.is_recording():
            span.set_attribute("invoiceops.gateway.alias", event.alias)
            span.set_attribute("invoiceops.gateway.status", event.status)
            span.set_attribute("invoiceops.gateway.attempts", event.attempts)
            span.set_attribute("invoiceops.gateway.route_index", event.route_index)
            span.set_attribute("invoiceops.gateway.cache_hit", event.cache_hit)
            span.set_attribute("invoiceops.gateway.latency_ms", event.latency_ms)
            span.set_attribute(
                "langfuse.observation.model.name", event.model or event.requested_model
            )
            span.set_attribute("langfuse.observation.metadata.model_version", event.model_version)
            span.set_attribute("gen_ai.request.model", event.requested_model)
            if event.model is not None:
                span.set_attribute("gen_ai.response.model", event.model)
            if event.usage is not None:
                span.set_attribute("gen_ai.usage.input_tokens", event.usage.input_tokens)
                span.set_attribute("gen_ai.usage.output_tokens", event.usage.output_tokens)
                span.set_attribute(
                    "langfuse.observation.usage_details",
                    json.dumps(
                        {
                            "input": event.usage.input_tokens,
                            "output": event.usage.output_tokens,
                            "total": event.usage.total_tokens,
                        },
                        separators=(",", ":"),
                    ),
                )
            if event.cost_usd is not None:
                span.set_attribute(
                    "langfuse.observation.cost_details",
                    '{"total":' + str(event.cost_usd) + "}",
                )
            if event.budget_alert:
                span.set_attribute("invoiceops.gateway.budget_alert", True)
            if event.status != "succeeded":
                span.set_attribute(
                    "invoiceops.gateway.error_code", event.error_code or event.status
                )
                span.set_status(Status(StatusCode.ERROR))
        self._delegate.record(event)
