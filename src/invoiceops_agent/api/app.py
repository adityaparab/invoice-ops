"""FastAPI shell with injected infrastructure and explicit resource ownership."""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated
from uuid import UUID

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.security import HTTPBearer
from prometheus_client import CONTENT_TYPE_LATEST
from pydantic import AwareDatetime
from starlette.responses import JSONResponse

from invoiceops_agent.api.audit_reader import AuditReader, PostgresAuditReader
from invoiceops_agent.api.auth_store import AuthStore, PostgresAuthStore, bearer_token
from invoiceops_agent.api.context import (
    IDEMPOTENCY_KEY_PATTERN,
    RequestContext,
    get_request_context,
)
from invoiceops_agent.api.dashboard_reader import DashboardReader, PostgresDashboardReader
from invoiceops_agent.api.decision_service import DecisionService, DecisionWriter
from invoiceops_agent.api.dependencies import (
    ApiRuntime,
    DependencyFactory,
    check_readiness,
    default_dependency_factory,
)
from invoiceops_agent.api.errors import install_error_handlers, problem_response
from invoiceops_agent.api.eval_reader import EvalReader, FileEvalReader
from invoiceops_agent.api.ingestion_dependencies import (
    UploadFactory,
    UploadRuntime,
    default_upload_factory,
)
from invoiceops_agent.api.invoice_reader import InvoiceReader, PostgresInvoiceReader
from invoiceops_agent.api.middleware import RequestContextMiddleware
from invoiceops_agent.api.provenance_reader import PostgresProvenanceReader, ProvenanceReader
from invoiceops_agent.api.read_auth import (
    authenticate_evals,
    authenticate_read,
    authenticate_run,
    authorize_auditor,
    authorize_queue,
    session_user,
)
from invoiceops_agent.api.run_progress_reader import PostgresRunProgressReader, RunProgressReader
from invoiceops_agent.api.schemas.audit import AuditRunPage
from invoiceops_agent.api.schemas.auth import (
    LoginRequest,
    LoginResponse,
    LogoutResponse,
    SessionUser,
)
from invoiceops_agent.api.schemas.dashboard import DashboardSummary
from invoiceops_agent.api.schemas.decision import DecisionRequest, DecisionResponse
from invoiceops_agent.api.schemas.email import email_request_schema
from invoiceops_agent.api.schemas.evals import EvalDashboard
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
from invoiceops_agent.api.schemas.provenance import InvoiceProvenancePage, RunTracePage
from invoiceops_agent.api.schemas.run_progress import RunProgress
from invoiceops_agent.api.settings import ApiSettings
from invoiceops_agent.api.uploads import authenticate_upload, parse_upload
from invoiceops_agent.api.webhooks import decode_email_document, read_signed_email
from invoiceops_agent.gateway_client.spend_logs import (
    SpendLogReader,
    SpendLogSampler,
    SpendLogSettings,
)
from invoiceops_agent.graph.ingestion import utc_now
from invoiceops_agent.obs.logging import configure_logging
from invoiceops_agent.obs.metrics import metrics_session
from invoiceops_agent.obs.tracing import tracing_session


def create_app(
    settings: ApiSettings | None = None,
    *,
    dependency_factory: DependencyFactory | None = None,
    upload_factory: UploadFactory | None = None,
    invoice_reader: InvoiceReader | None = None,
    decision_writer: DecisionWriter | None = None,
    dashboard_reader: DashboardReader | None = None,
    run_progress_reader: RunProgressReader | None = None,
    audit_reader: AuditReader | None = None,
    provenance_reader: ProvenanceReader | None = None,
    eval_reader: EvalReader | None = None,
    auth_store: AuthStore | None = None,
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
    decisions = decision_writer if decision_writer is not None else DecisionService(configuration)
    dashboard = (
        dashboard_reader if dashboard_reader is not None else PostgresDashboardReader(configuration)
    )
    run_progress = (
        run_progress_reader
        if run_progress_reader is not None
        else PostgresRunProgressReader(configuration)
    )
    audit = audit_reader if audit_reader is not None else PostgresAuditReader(configuration)
    provenance = (
        provenance_reader
        if provenance_reader is not None
        else PostgresProvenanceReader(configuration)
    )
    evals = eval_reader if eval_reader is not None else FileEvalReader(configuration)
    authentication = auth_store if auth_store is not None else PostgresAuthStore(configuration)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with (
            tracing_session("invoiceops-api"),
            metrics_session("invoiceops-api", prometheus=True) as metrics,
            httpx.AsyncClient(timeout=3, follow_redirects=False, trust_env=False) as spend_client,
            factory(configuration) as checks,
            ingestion_factory(configuration) as ingestion,
        ):
            spend_settings = SpendLogSettings()
            spend_reader = (
                SpendLogReader(spend_settings, spend_client)
                if spend_settings.api_base is not None and spend_settings.master_key is not None
                else None
            )
            app.state.spend_sampler = SpendLogSampler(spend_reader, metrics.registry)
            app.state.metrics = metrics
            runtime.checks = checks
            uploads.service = ingestion
            try:
                yield
            finally:
                runtime.checks = None
                uploads.service = None
                app.state.spend_sampler = None
                app.state.metrics = None

    app = FastAPI(title="InvoiceOps API", version="0.1.0", lifespan=lifespan)
    app.state.auth_store = authentication
    app.add_middleware(RequestContextMiddleware)
    install_error_handlers(app)

    @app.post(
        "/v1/auth/login",
        response_model=LoginResponse,
        tags=["auth"],
        responses={status: {"model": ProblemDetails} for status in (400, 401, 409, 422, 503)},
    )
    async def login(
        credentials: LoginRequest,
        context: Annotated[RequestContext, Depends(get_request_context)],
        response: Response,
    ) -> LoginResponse:
        if context.idempotency_key is None:
            raise HTTPException(400, "An Idempotency-Key is required.")
        response.headers["Cache-Control"] = "no-store"
        return await authentication.login(
            credentials.email, credentials.password, context.idempotency_key
        )

    @app.get("/v1/auth/me", response_model=SessionUser, tags=["auth"])
    async def current_user(request: Request) -> SessionUser:
        user = await session_user(request)
        if user is None:
            raise HTTPException(401, "A login session is required.")
        return user

    @app.post("/v1/auth/logout", response_model=LogoutResponse, tags=["auth"])
    async def logout(request: Request) -> LogoutResponse:
        token = bearer_token(request.headers.getlist("Authorization"))
        if token is None or not token.startswith("io_") or len(token) != 67:
            raise HTTPException(401, "A login session is required.")
        await authentication.logout(token)
        return LogoutResponse()

    @app.get("/v1/metrics", include_in_schema=False)
    async def prometheus_metrics() -> Response:
        metrics = app.state.metrics if hasattr(app.state, "metrics") else None
        if metrics is None:
            raise HTTPException(503, "Metrics are not ready.")
        await app.state.spend_sampler.refresh()
        return Response(metrics.render(), media_type=CONTENT_TYPE_LATEST)

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
        await authenticate_upload(request, configuration)
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
        "/v1/dashboard",
        response_model=DashboardSummary,
        tags=["dashboard"],
        dependencies=[Depends(HTTPBearer(auto_error=False, scheme_name="PersonaToken"))],
        responses={status: {"model": ProblemDetails} for status in (401, 403, 422, 503)},
    )
    async def get_dashboard(
        request: Request,
        period_days: Annotated[int, Query(ge=1, le=90)] = 30,
    ) -> DashboardSummary:
        if await authenticate_read(request, configuration) != "MANAGER":
            raise HTTPException(403, "Only the procurement manager can read the dashboard.")
        return await dashboard.summary(period_days)

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
        authorize_queue(await authenticate_read(request, configuration))
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
        await authenticate_read(request, configuration)
        return await reads.detail(invoice_id)

    @app.get(
        "/v1/runs/{run_id}/progress",
        response_model=RunProgress,
        tags=["runs"],
        dependencies=[Depends(HTTPBearer(auto_error=False, scheme_name="PersonaToken"))],
        responses={status: {"model": ProblemDetails} for status in (401, 404, 422, 503)},
    )
    async def get_run_progress(run_id: UUID, request: Request) -> RunProgress:
        await authenticate_run(request, configuration)
        return await run_progress.read(run_id)

    @app.get(
        "/v1/runs/{run_id}/ledger",
        response_model=AuditRunPage,
        tags=["audit"],
        dependencies=[Depends(HTTPBearer(auto_error=False, scheme_name="PersonaToken"))],
        responses={status: {"model": ProblemDetails} for status in (401, 403, 404, 422, 503)},
    )
    async def get_run_ledger(
        run_id: UUID,
        request: Request,
        context: Annotated[RequestContext, Depends(get_request_context)],
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        after_sequence: Annotated[int | None, Query(ge=1)] = None,
    ) -> AuditRunPage:
        authorize_auditor(await authenticate_read(request, configuration))
        return await audit.for_run(
            run_id, trace_id=context.trace_id, limit=limit, after_sequence=after_sequence
        )

    @app.get(
        "/v1/runs/{run_id}/trace",
        response_model=RunTracePage,
        tags=["audit"],
        dependencies=[Depends(HTTPBearer(auto_error=False, scheme_name="PersonaToken"))],
        responses={status: {"model": ProblemDetails} for status in (401, 403, 404, 422, 503)},
    )
    async def get_run_trace(
        run_id: UUID,
        request: Request,
        context: Annotated[RequestContext, Depends(get_request_context)],
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        after_sequence: Annotated[int | None, Query(ge=1)] = None,
    ) -> RunTracePage:
        authorize_auditor(await authenticate_read(request, configuration))
        return await provenance.for_run_trace(
            run_id, trace_id=context.trace_id, limit=limit, after_sequence=after_sequence
        )

    @app.get(
        "/v1/invoices/{invoice_id}/provenance",
        response_model=InvoiceProvenancePage,
        tags=["audit"],
        dependencies=[Depends(HTTPBearer(auto_error=False, scheme_name="PersonaToken"))],
        responses={status: {"model": ProblemDetails} for status in (400, 401, 403, 404, 422, 503)},
    )
    async def get_invoice_provenance(
        invoice_id: UUID,
        request: Request,
        context: Annotated[RequestContext, Depends(get_request_context)],
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        after_created_at: AwareDatetime | None = None,
        after_event_id: UUID | None = None,
    ) -> InvoiceProvenancePage:
        authorize_auditor(await authenticate_read(request, configuration))
        if (after_created_at is None) != (after_event_id is None):
            raise HTTPException(400, "Both provenance cursor fields are required together.")
        return await provenance.for_invoice(
            invoice_id,
            trace_id=context.trace_id,
            limit=limit,
            after_created_at=after_created_at,
            after_event_id=after_event_id,
        )

    @app.get(
        "/v1/evals/reports",
        response_model=EvalDashboard,
        tags=["evals"],
        dependencies=[Depends(HTTPBearer(auto_error=False, scheme_name="PersonaToken"))],
        responses={status: {"model": ProblemDetails} for status in (401, 403, 503)},
    )
    async def get_eval_reports(request: Request) -> EvalDashboard:
        await authenticate_evals(request, configuration)
        return await evals.dashboard()

    @app.post(
        "/v1/exceptions/{exception_id}/decision",
        response_model=DecisionResponse,
        status_code=201,
        tags=["exceptions"],
        dependencies=[Depends(HTTPBearer(auto_error=False, scheme_name="PersonaToken"))],
        responses={
            status: {"model": ProblemDetails} for status in (400, 401, 403, 404, 409, 422, 503)
        },
        openapi_extra={
            "parameters": [
                {
                    "name": "Idempotency-Key",
                    "in": "header",
                    "required": True,
                    "schema": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 128,
                        "pattern": IDEMPOTENCY_KEY_PATTERN,
                    },
                }
            ]
        },
    )
    async def submit_exception_decision(
        exception_id: UUID,
        request: Request,
        decision: DecisionRequest,
        context: Annotated[RequestContext, Depends(get_request_context)],
    ) -> DecisionResponse:
        role = await authenticate_read(request, configuration)
        if role == "AUDITOR":
            raise HTTPException(403, "Auditors cannot make exception decisions.")
        if context.idempotency_key is None:
            raise HTTPException(400, "An Idempotency-Key is required.")
        return await decisions.submit(
            exception_id=exception_id,
            request=decision,
            role=role,
            idempotency_key=context.idempotency_key,
            trace_id=context.trace_id,
        )

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
