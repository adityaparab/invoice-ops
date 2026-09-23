"""Validated request metadata and shared idempotency-key syntax."""

from dataclasses import dataclass

from starlette.requests import Request

IDEMPOTENCY_KEY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"


@dataclass(frozen=True)
class RequestContext:
    trace_id: str
    idempotency_key: str | None


def get_request_context(request: Request) -> RequestContext:
    """Expose validated metadata without inventing persistence or replay semantics."""
    context = getattr(request.state, "context", None)
    if not isinstance(context, RequestContext):
        raise RuntimeError("Request context middleware was not installed")
    return context
