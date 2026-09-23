"""Sanitized RFC 7807 responses at the HTTP boundary."""

import logging
from collections.abc import Mapping
from http import HTTPStatus

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse

from invoiceops_agent.api.context import get_request_context
from invoiceops_agent.api.schemas.health import DependencyStatuses
from invoiceops_agent.api.schemas.problem import ProblemDetails

logger = logging.getLogger(__name__)


def problem_response(
    request: Request,
    *,
    status: int,
    detail: str,
    headers: Mapping[str, str] | None = None,
    dependencies: DependencyStatuses | None = None,
) -> JSONResponse:
    """Serialize one shared problem contract, including when middleware rejects a request."""
    trace_id = get_request_context(request).trace_id
    try:
        title = HTTPStatus(status).phrase
    except ValueError:
        title = "HTTP error"
    problem = ProblemDetails(
        title=title,
        status=status,
        detail=detail,
        instance=request.url.path,
        trace_id=trace_id,
        dependencies=dependencies,
    )
    return JSONResponse(
        problem.model_dump(exclude_none=True),
        status_code=status,
        media_type="application/problem+json",
        headers={**(headers or {}), "X-Trace-ID": trace_id},
    )


async def _http_error(request: Request, error: Exception) -> JSONResponse:
    if not isinstance(error, HTTPException):
        raise TypeError("HTTP exception handler received an incompatible exception")
    return problem_response(
        request,
        status=error.status_code,
        detail=str(error.detail),
        headers=error.headers,
    )


async def _validation_error(request: Request, error: Exception) -> JSONResponse:
    return problem_response(
        request, status=422, detail="The request did not match the required schema."
    )


async def _unexpected_error(request: Request, error: Exception) -> JSONResponse:
    logger.error(
        "request_failed trace_id=%s error_type=%s",
        get_request_context(request).trace_id,
        type(error).__name__,
    )
    return problem_response(request, status=500, detail="An unexpected server error occurred.")


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(HTTPException, _http_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(Exception, _unexpected_error)
