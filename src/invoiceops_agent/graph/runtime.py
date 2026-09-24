"""Own one invoice run's gateway, storage, audit, and checkpoint resources."""

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, date, datetime
from uuid import UUID

import httpx2
import psycopg
from psycopg.rows import DictRow, dict_row
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from invoiceops_agent.agents.extraction import ExtractionAgent
from invoiceops_agent.agents.near_duplicate import NearDuplicateAgent
from invoiceops_agent.agents.near_duplicate_settings import LiteLLMWorkflowSettings
from invoiceops_agent.agents.triage import TriageAgent
from invoiceops_agent.gateway_client import GatewayClient
from invoiceops_agent.gateway_client.telemetry import GatewayTelemetry
from invoiceops_agent.graph.checkpoints import postgres_invoice_graph
from invoiceops_agent.graph.errors import RunNotFound
from invoiceops_agent.graph.invoice_nodes import InvoiceNodes
from invoiceops_agent.graph.invoice_runner import InvoiceGraphRunner
from invoiceops_agent.graph.live_services import ConnectionFactory, LiveInvoiceServices
from invoiceops_agent.graph.nodes.exception_taxonomy import ExceptionTaxonomyNode
from invoiceops_agent.graph.nodes.match3way import Match3WayNode
from invoiceops_agent.graph.nodes.policy import PolicyNode
from invoiceops_agent.graph.nodes.validate import ValidateNode
from invoiceops_agent.graph.settings import GraphSettings
from invoiceops_agent.graph.state import InvoiceGraphState, ReviewDecision
from invoiceops_agent.ledger.audit import TransactionalAuditSink
from invoiceops_agent.ledger.settings import LedgerSettings
from invoiceops_agent.ledger.writer import LedgerWriter
from invoiceops_agent.schemas.gate import CompositeGateConfig
from invoiceops_agent.tools.document_preflight import DocumentPreflight
from invoiceops_agent.tools.document_settings import DocumentSettings
from invoiceops_agent.tools.raw_document_reader import S3DocumentReader
from invoiceops_agent.tools.raw_storage import s3_storage
from invoiceops_agent.tools.semantic_cache import PostgresSemanticCache
from invoiceops_agent.tools.storage_settings import StorageSettings


class InvoiceRuntimeSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="INVOICEOPS_", env_file=".env", extra="ignore", hide_input_in_errors=True
    )

    postgres_dsn: SecretStr
    auto_approval_enabled: bool = True


def utc_now() -> datetime:
    return datetime.now(UTC)


@asynccontextmanager
async def runtime_connection(dsn: str) -> AsyncIterator[psycopg.AsyncConnection[DictRow]]:
    async with await psycopg.AsyncConnection.connect(
        dsn,
        autocommit=True,
        row_factory=dict_row,
        connect_timeout=5,
        options="-c statement_timeout=10000 -c lock_timeout=10000",
    ) as connection:
        yield connection


class InvoiceWorkflowRuntime:
    def __init__(
        self,
        runner: InvoiceGraphRunner,
        initial: InvoiceGraphState,
    ) -> None:
        self.runner = runner
        self.initial = initial

    async def run(self) -> InvoiceGraphState:
        return await self.runner.run(self.initial)

    async def resume(self, decision: ReviewDecision) -> InvoiceGraphState:
        return await self.runner.resume(
            run_id=self.initial.run_id,
            invoice_id=self.initial.invoice_id,
            trace_id=self.initial.trace_id,
            decision=decision,
        )


async def load_invoice_state(
    connection: ConnectionFactory, run_id: UUID, *, as_of: date
) -> tuple[InvoiceGraphState, str]:
    async with connection() as database:
        cursor = await database.execute(
            "SELECT r.invoice_id, r.trace_id, r.graph_version, i.raw_ref, "
            "i.content_hash, i.content_type "
            "FROM public.runs r JOIN public.invoices i ON i.id = r.invoice_id "
            "WHERE r.id = %s",
            (run_id,),
        )
        row = await cursor.fetchone()
    if row is None:
        raise RunNotFound("Invoice workflow run is absent", run_id=run_id)
    graph_version = row["graph_version"]
    if graph_version not in {"ingestion-v1", "invoice-v1"}:
        raise RunNotFound("Invoice run has an unsupported graph version", run_id=run_id)
    return (
        InvoiceGraphState.model_validate(
            {
                "run_id": run_id,
                "invoice_id": row["invoice_id"],
                "trace_id": row["trace_id"],
                "as_of": as_of,
                "raw_ref": row["raw_ref"],
                "content_hash": row["content_hash"],
                "content_type": row["content_type"],
            }
        ),
        graph_version,
    )


@asynccontextmanager
async def invoice_runtime(
    run_id: UUID,
    *,
    settings: InvoiceRuntimeSettings | None = None,
    clock: Callable[[], datetime] = utc_now,
    gateway_telemetry: GatewayTelemetry | None = None,
    gateway_transport: httpx2.AsyncBaseTransport | None = None,
) -> AsyncIterator[InvoiceWorkflowRuntime]:
    runtime = settings if settings is not None else InvoiceRuntimeSettings()
    graph = GraphSettings(_env_file=".env")
    storage = StorageSettings(_env_file=".env")
    document = DocumentSettings(_env_file=".env")
    litellm = LiteLLMWorkflowSettings()
    if (
        storage.minio_url is None
        or storage.minio_access_key is None
        or storage.minio_secret_key is None
    ):
        raise ValueError("Invoice workflow requires explicit MinIO settings")

    def connection() -> AbstractAsyncContextManager[psycopg.AsyncConnection[DictRow]]:
        return runtime_connection(runtime.postgres_dsn.get_secret_value())

    initial, run_graph_version = await load_invoice_state(connection, run_id, as_of=clock().date())
    writer = LedgerWriter(
        LedgerSettings(
            graph_version=run_graph_version,
            model_version="not-applicable@v1",
            prompt_version="not-applicable@v1",
            policy_version="not-applicable@v1",
            _env_file=None,
        ),
        clock=clock,
    )
    audit = TransactionalAuditSink(connection, writer)
    async with (
        s3_storage(
            endpoint=str(storage.minio_url),
            access_key=storage.minio_access_key.get_secret_value(),
            secret_key=storage.minio_secret_key.get_secret_value(),
            bucket=storage.raw_bucket,
            timeout_seconds=storage.storage_timeout_seconds,
        ) as raw_storage,
        GatewayClient(
            litellm.gateway_settings(),
            transport=gateway_transport,
            telemetry=gateway_telemetry,
            semantic_cache=PostgresSemanticCache(connection),
        ) as gateway,
    ):
        services = LiveInvoiceServices(
            connection=connection,
            extraction=ExtractionAgent(
                S3DocumentReader(raw_storage.client, storage.raw_bucket, document),
                DocumentPreflight(document),
                gateway,
                audit,
            ),
            validation=ValidateNode(audit),
            matching=Match3WayNode(audit),
            similarity=NearDuplicateAgent(gateway, connection, writer),
            taxonomy=ExceptionTaxonomyNode(audit),
            policy=PolicyNode(audit),
            audit=audit,
            audit_writer=writer,
            gate_config=CompositeGateConfig(auto_approval_enabled=runtime.auto_approval_enabled),
            triage_agent=TriageAgent(gateway),
            clock=clock,
        )
        async with postgres_invoice_graph(graph, InvoiceNodes(services)) as runner:
            yield InvoiceWorkflowRuntime(runner, initial)
