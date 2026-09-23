"""One sanitized outcome per logical call; Phase 4 can adapt it to a trace span."""

import logging
from decimal import Decimal
from typing import Literal, Protocol

from invoiceops_agent.gateway_client.schemas import ModelAlias, RequestContext, TokenUsage, Version

logger = logging.getLogger(__name__)


class GatewayEvent(RequestContext):
    alias: ModelAlias
    model: Version | None = None
    model_version: Version
    status: Literal["succeeded", "failed", "cancelled"]
    attempts: int
    latency_ms: float
    error_code: str | None = None
    usage: TokenUsage | None = None
    cost_usd: Decimal | None = None


class GatewayTelemetry(Protocol):
    def record(self, event: GatewayEvent) -> None:
        """Record metadata only; implementations must not throw or block on I/O."""


class LoggingTelemetry:
    def record(self, event: GatewayEvent) -> None:
        logger.info(
            "event=gateway.call run_id=%s trace_id=%s alias=%s model=%s model_version=%s "
            "prompt_version=%s status=%s attempts=%d latency_ms=%.1f "
            "input_tokens=%s output_tokens=%s cost_usd=%s error_code=%s",
            event.run_id,
            event.trace_id,
            event.alias,
            event.model,
            event.model_version,
            event.prompt_version,
            event.status,
            event.attempts,
            event.latency_ms,
            event.usage.input_tokens if event.usage else None,
            event.usage.output_tokens if event.usage else None,
            event.cost_usd,
            event.error_code,
        )
