"""Typed request metadata made available to future mutation dependencies."""

from dataclasses import dataclass

from starlette.requests import Request


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
