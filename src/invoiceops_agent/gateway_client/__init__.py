"""Typed, guarded access to the configured LiteLLM gateway."""

from invoiceops_agent.gateway_client.client import GatewayClient
from invoiceops_agent.gateway_client.errors import (
    GatewayCassetteMismatch,
    GatewayConfigurationError,
    GatewayDeadlineExceeded,
    GatewayError,
    GatewayRequestRejected,
    GatewayUnavailable,
    GuardrailRejected,
    InvalidGatewayResponse,
    InvalidStructuredOutput,
    TokenBudgetExceeded,
)
from invoiceops_agent.gateway_client.schemas import (
    EmbeddingRequest,
    EmbeddingValue,
    FilePart,
    GatewayMessage,
    GatewayProvenance,
    GatewayRequest,
    GatewayResult,
    ImagePart,
    TextPart,
    TokenUsage,
)
from invoiceops_agent.gateway_client.settings import AliasPolicy, GatewaySettings

__all__ = [
    "AliasPolicy",
    "EmbeddingRequest",
    "EmbeddingValue",
    "FilePart",
    "GatewayCassetteMismatch",
    "GatewayClient",
    "GatewayConfigurationError",
    "GatewayDeadlineExceeded",
    "GatewayError",
    "GatewayMessage",
    "GatewayProvenance",
    "GatewayRequest",
    "GatewayRequestRejected",
    "GatewayResult",
    "GatewaySettings",
    "GatewayUnavailable",
    "GuardrailRejected",
    "ImagePart",
    "InvalidGatewayResponse",
    "InvalidStructuredOutput",
    "TextPart",
    "TokenBudgetExceeded",
    "TokenUsage",
]
