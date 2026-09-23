"""Pure ASGI request correlation and mutation-key validation."""

import logging
import re
from time import perf_counter
from uuid import uuid4

from starlette.datastructures import Headers, MutableHeaders
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from invoiceops_agent.api.context import IDEMPOTENCY_KEY_PATTERN, RequestContext
from invoiceops_agent.api.errors import problem_response

logger = logging.getLogger(__name__)
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})
_TRACE_ID = re.compile(r"[0-9a-f]{32}")
_IDEMPOTENCY_KEY = re.compile(IDEMPOTENCY_KEY_PATTERN)


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = perf_counter()
        headers = Headers(scope=scope)
        supplied_trace_ids = headers.getlist("X-Trace-ID")
        trace_id = (
            supplied_trace_ids[0]
            if len(supplied_trace_ids) == 1
            and _TRACE_ID.fullmatch(supplied_trace_ids[0])
            and supplied_trace_ids[0] != "0" * 32
            else uuid4().hex
        )
        supplied_keys = headers.getlist("Idempotency-Key")
        valid_key = (
            supplied_keys[0]
            if len(supplied_keys) == 1 and _IDEMPOTENCY_KEY.fullmatch(supplied_keys[0])
            else None
        )
        scope.setdefault("state", {})["context"] = RequestContext(trace_id, valid_key)
        status = 500

        async def send_with_trace(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)["X-Trace-ID"] = trace_id
                status = message["status"]
            await send(message)

        try:
            if scope["method"] not in _SAFE_METHODS and valid_key is None:
                response = problem_response(
                    Request(scope),
                    status=400,
                    detail=(
                        "Supply one Idempotency-Key containing 1–128 ASCII letters, digits, "
                        "dots, underscores, colons, or hyphens, starting with a letter or digit."
                    ),
                )
                await response(scope, receive, send_with_trace)
            else:
                await self.app(scope, receive, send_with_trace)
        finally:
            logger.info(
                "request_completed method=%s status=%s trace_id=%s duration_ms=%.3f",
                scope["method"],
                status,
                trace_id,
                (perf_counter() - started) * 1000,
            )
