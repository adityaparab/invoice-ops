"""FastAPI shell with injected infrastructure and explicit resource ownership."""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.security import HTTPBearer
from starlette.responses import JSONResponse

from invoiceops_agent.api.context import (
    IDEMPOTENCY_KEY_PATTERN,
    RequestContext,
    get_request_context,
)
from invoiceops_agent.api.dependencies import (
    ApiRuntime,
    DependencyFactory,
    check_readiness,
    default_dependency_factory,
)
from invoiceops_agent.api.errors import install_error_handlers, problem_response
from invoiceops_agent.api.ingestion_dependencies import (
    UploadFactory,
    UploadRuntime,
    default_upload_factory,
)
from invoiceops_agent.api.invoice_reader import InvoiceReader, PostgresInvoiceReader
from invoiceops_agent.api.middleware import RequestContextMiddleware
from invoiceops_agent.api.read_auth import authenticate_read, authorize_queue
from invoiceops_agent.api.schemas.email import email_request_schema
from invoiceops_agent.api.schemas.health import (
    DependencyStatuses,
    LivenessResponse,
    ReadinessResponse,
)
from invoiceops_agent.api.schemas.invoice import InvoiceUploadResponse
from invoiceops_agent.api.schemas.invoice_read import (
    InvoiceDetail,
    InvoiceListQuery,
    InvoicePage,
    InvoiceSource,
    InvoiceStatus,
    RunStatus,
)
from invoiceops_agent.api.schemas.problem import ProblemDetails
from invoiceops_agent.api.settings import ApiSettings
from invoiceops_agent.api.uploads import authenticate_upload, parse_upload
from invoiceops_agent.api.webhooks import decode_email_document, read_signed_email
from invoiceops_agent.graph.ingestion import utc_now
from invoiceops_agent.obs.logging import configure_logging


def create_app(
    settings: ApiSettings | None = None,
    *,
    dependency_factory: DependencyFactory | None = None,
    upload_factory: UploadFactory | None = None,
    invoice_reader: InvoiceReader | None = None,
    webhook_clock: Callable[[], datetime] = utc_now,
) -> FastAPI:
    """Build a fresh app; external resources are allocated only during ASGI lifespan."""
    configure_logging()
    configuration = settings if settings is not None else ApiSettings()
    factory = dependency_factory if dependency_factory is not None else default_dependency_factory
    runtime = ApiRuntime()
    uploads = UploadRuntime()
    ingestion_factory = upload_factory if upload_factory is not None else default_upload_factory
    reads = invoice_reader if invoice_reader is not None else PostgresInvoiceReader(configuration)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with factory(configuration) as checks, ingestion_factory(configuration) as ingestion:
            runtime.checks = checks
            uploads.service = ingestion
            try:
                yield
            finally:
                runtime.checks = None
                uploads.service = None

    app = FastAPI(title="InvoiceOps API", version="0.1.0", lifespan=lifespan)
    app.add_middleware(RequestContextMiddleware)
    install_error_handlers(app)

    @app.post(
        "/v1/invoices",
        response_model=InvoiceUploadResponse,
        status_code=201,
        tags=["invoices"],
        # Declare the scheme while our checker additionally rejects duplicate auth headers.
        dependencies=[Depends(HTTPBearer(auto_error=False, scheme_name="ServiceToken"))],
        responses={
            **{status: {"model": ProblemDetails} for status in (400, 401, 408, 409, 413, 415, 503)},
            200: {
                "model": InvoiceUploadResponse,
                "description": "Existing content rejected as duplicate",
            },
        },
        openapi_extra={
            "parameters": [
                {
                    "name": "Idempotency-Key",
                    "in": "header",
                    "required": True,
                    "description": "Stable replay key, validated before body reading.",
                    "schema": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 128,
                        "pattern": IDEMPOTENCY_KEY_PATTERN,
                    },
                }
            ],
            "requestBody": {
                "required": True,
                "content": {
                    "multipart/form-data": {
                        "schema": {
                            "type": "object",
                            "required": ["file"],
                            "additionalProperties": False,
                            "properties": {"file": {"type": "string", "format": "binary"}},
                        }
                    }
                },
            },
        },
    )
    async def upload_invoice(
        request: Request,
        response: Response,
        context: Annotated[RequestContext, Depends(get_request_context)],
    ) -> InvoiceUploadResponse:
        authenticate_upload(request, configuration)
        if uploads.service is None:
            raise HTTPException(503, "Invoice uploads are not configured.")
        document = await parse_upload(request, configuration)
        if context.idempotency_key is None:
            raise HTTPException(400, "An Idempotency-Key is required.")
        outcome = await uploads.service.ingest(
            document, key=context.idempotency_key, trace_id=context.trace_id
        )
        response.status_code = outcome.response_status
        return InvoiceUploadResponse.model_validate(outcome.body.model_dump())

    @app.post(
        "/v1/invoices/email-webhook",
        response_model=InvoiceUploadResponse,
        status_code=201,
        tags=["invoices"],
        responses={
            **{status: {"model": ProblemDetails} for status in (400, 401, 408, 409, 413, 415, 503)},
            200: {
                "model": InvoiceUploadResponse,
                "description": "Existing content rejected as duplicate",
            },
        },
        openapi_extra={
            "parameters": [
                {
                    "name": "Idempotency-Key",
                    "in": "header",
                    "required": True,
                    "description": "Stable replay key, validated before body reading.",
                    "schema": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 128,
                        "pattern": IDEMPOTENCY_KEY_PATTERN,
                    },
                },
                *[
                    {"name": name, "in": "header", "required": True, "schema": {"type": "string"}}
                    for name in (
                        "X-Webhook-Timestamp",
                        "X-Webhook-Nonce",
                        "X-Webhook-Signature",
                    )
                ],
            ],
            "requestBody": {
                "required": True,
                "content": {"application/json": {"schema": email_request_schema()}},
            },
        },
    )
    async def email_webhook(
        request: Request,
        response: Response,
        context: Annotated[RequestContext, Depends(get_request_context)],
    ) -> InvoiceUploadResponse:
        if context.idempotency_key is None:
            raise HTTPException(400, "An Idempotency-Key is required.")
        envelope, nonce = await read_signed_email(request, configuration, clock=webhook_clock)
        if uploads.service is None:
            raise HTTPException(503, "Invoice ingestion is not configured.")
        document = await decode_email_document(envelope, max_bytes=configuration.document_max_bytes)
        outcome = await uploads.service.ingest(
            document, key=context.idempotency_key, trace_id=context.trace_id, nonce=nonce
        )
        response.status_code = outcome.response_status
        return InvoiceUploadResponse.model_validate(outcome.body.model_dump())

    @app.get(
        "/v1/invoices",
        response_model=InvoicePage,
        tags=["invoices"],
        dependencies=[Depends(HTTPBearer(auto_error=False, scheme_name="PersonaToken"))],
        responses={status: {"model": ProblemDetails} for status in (400, 401, 403, 422, 503)},
    )
    async def list_invoices(
        request: Request,
        status: InvoiceStatus | None = None,
        run_status: RunStatus | None = None,
        source: InvoiceSource | None = None,
        exception_only: bool = False,
        min_priority: Annotated[int | None, Query(ge=0, le=3)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        cursor: Annotated[str | None, Query(max_length=200)] = None,
    ) -> InvoicePage:
        authorize_queue(authenticate_read(request, configuration))
        return await reads.list(
            InvoiceListQuery(
                status=status,
                run_status=run_status,
                source=source,
                exception_only=exception_only,
                min_priority=min_priority,
                limit=limit,
                cursor=cursor,
            )
        )

    @app.get(
        "/v1/invoices/{invoice_id}",
        response_model=InvoiceDetail,
        tags=["invoices"],
        dependencies=[Depends(HTTPBearer(auto_error=False, scheme_name="PersonaToken"))],
        responses={status: {"model": ProblemDetails} for status in (401, 404, 422, 503)},
    )
    async def get_invoice(invoice_id: UUID, request: Request) -> InvoiceDetail:
        authenticate_read(request, configuration)
        return await reads.detail(invoice_id)

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
