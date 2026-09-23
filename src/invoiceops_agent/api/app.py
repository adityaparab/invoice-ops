"""FastAPI shell with injected infrastructure and explicit resource ownership."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Request
from starlette.responses import JSONResponse

from invoiceops_agent.api.context import RequestContext, get_request_context
from invoiceops_agent.api.dependencies import (
    ApiRuntime,
    DependencyFactory,
    check_readiness,
    default_dependency_factory,
)
from invoiceops_agent.api.errors import install_error_handlers, problem_response
from invoiceops_agent.api.middleware import RequestContextMiddleware
from invoiceops_agent.api.schemas.health import (
    DependencyStatuses,
    LivenessResponse,
    ReadinessResponse,
)
from invoiceops_agent.api.schemas.problem import ProblemDetails
from invoiceops_agent.api.settings import ApiSettings


def create_app(
    settings: ApiSettings | None = None, *, dependency_factory: DependencyFactory | None = None
) -> FastAPI:
    """Build a fresh app; external resources are allocated only during ASGI lifespan."""
    configuration = settings if settings is not None else ApiSettings()
    factory = dependency_factory if dependency_factory is not None else default_dependency_factory
    runtime = ApiRuntime()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with factory(configuration) as checks:
            runtime.checks = checks
            try:
                yield
            finally:
                runtime.checks = None

    app = FastAPI(title="InvoiceOps API", version="0.1.0", lifespan=lifespan)
    app.add_middleware(RequestContextMiddleware)
    install_error_handlers(app)

    @app.get("/healthz", response_model=LivenessResponse, tags=["health"])
    async def healthz() -> LivenessResponse:
        return LivenessResponse()

    @app.get(
        "/readyz",
        response_model=ReadinessResponse,
        responses={
            503: {
                "model": ProblemDetails,
                "description": "Required infrastructure is unavailable or unconfigured",
                "content": {
                    "application/problem+json": {
                        "schema": {"$ref": "#/components/schemas/ProblemDetails"}
                    }
                },
            }
        },
        tags=["health"],
    )
    async def readyz(
        request: Request, context: Annotated[RequestContext, Depends(get_request_context)]
    ) -> ReadinessResponse | JSONResponse:
        if runtime.checks is None:
            statuses = DependencyStatuses(postgres="unavailable", minio="unavailable")
        else:
            statuses = await check_readiness(
                runtime.checks,
                timeout_seconds=configuration.readiness_timeout_seconds,
                trace_id=context.trace_id,
            )
        if statuses.postgres != "ok" or statuses.minio != "ok":
            return problem_response(
                request,
                status=503,
                detail="One or more required dependencies are not ready.",
                dependencies=statuses,
            )
        return ReadinessResponse(dependencies=statuses)

    return app
