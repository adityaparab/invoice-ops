"""Sanitized failures: callers can escalate without exposing SDK bodies or document text."""

from decimal import Decimal

from invoiceops_agent.gateway_client.schemas import RequestContext


class GatewayError(Exception):
    code = "gateway_error"

    def __init__(
        self, context: RequestContext, *, attempts: int = 0, cost_usd: Decimal | None = None
    ) -> None:
        self.run_id = context.run_id
        self.trace_id = context.trace_id
        self.attempts = attempts
        self.cost_usd = cost_usd
        super().__init__(f"{self.code} run_id={self.run_id} trace_id={self.trace_id}")


class GatewayConfigurationError(GatewayError):
    code = "gateway_configuration"


class GuardrailRejected(GatewayError):
    code = "gateway_guardrail_rejected"


class TokenBudgetExceeded(GatewayError):
    code = "gateway_budget_exceeded"


class GatewayUnavailable(GatewayError):
    code = "gateway_unavailable"


class GatewayDeadlineExceeded(GatewayUnavailable):
    code = "gateway_deadline_exceeded"


class GatewayRequestRejected(GatewayError):
    code = "gateway_request_rejected"


class InvalidGatewayResponse(GatewayError):
    code = "gateway_invalid_response"


class InvalidStructuredOutput(InvalidGatewayResponse):
    """The model returned malformed JSON or violated the requested Pydantic schema."""

    code = "gateway_invalid_structured_output"


class GatewayCassetteMismatch(GatewayError):
    code = "gateway_cassette_mismatch"
