"""Real audit and ERP writes across the invoice graph with offline model responses."""

from datetime import date
from typing import cast
from uuid import UUID

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import BaseModel
from tests.integration.support import INVOICE_ID, RUN_ID
from tests.integration.test_ledger import ledger_runtime_dsn as ledger_runtime_dsn
from tests.integration.test_ledger import runtime_connection
from tests.unit.matching_support import matching_request, snapshot_for

from invoiceops_agent.agents.extraction import ExtractionAgent
from invoiceops_agent.agents.near_duplicate import NearDuplicateAgent
from invoiceops_agent.gateway_client.schemas import (
    EmbeddingRequest,
    EmbeddingValue,
    GatewayProvenance,
    GatewayRequest,
    GatewayResult,
    RequestContext,
    TokenUsage,
)
from invoiceops_agent.gateway_client.settings import AliasPolicy
from invoiceops_agent.graph.checkpoints import InProcessRunLock, restricted_serializer
from invoiceops_agent.graph.invoice import build_invoice_graph
from invoiceops_agent.graph.invoice_nodes import InvoiceNodes
from invoiceops_agent.graph.invoice_runner import InvoiceGraphRunner
from invoiceops_agent.graph.live_services import (
    LiveInvoiceServices,
    ReplayEvidenceError,
    WorkflowTransitions,
)
from invoiceops_agent.graph.nodes.exception_taxonomy import ExceptionTaxonomyNode
from invoiceops_agent.graph.nodes.match3way import Match3WayNode
from invoiceops_agent.graph.nodes.policy import PolicyNode
from invoiceops_agent.graph.nodes.validate import ValidateNode
from invoiceops_agent.graph.retry import RetryConfig
from invoiceops_agent.graph.runtime import load_invoice_state
from invoiceops_agent.graph.state import InvoiceGraphState
from invoiceops_agent.graph.worker import PostgresAttemptStore, RetryWorker
from invoiceops_agent.ledger.audit import TransactionalAuditSink
from invoiceops_agent.ledger.reader import LedgerReader
from invoiceops_agent.ledger.settings import LedgerSettings
from invoiceops_agent.ledger.writer import LedgerWriter
from invoiceops_agent.schemas.documents import DocumentReference
from invoiceops_agent.schemas.extraction import InvoiceExtraction
from invoiceops_agent.schemas.gate import CompositeGateConfig, CompositeGateResult
from invoiceops_agent.tools.document_preflight import DocumentPreflight
from invoiceops_agent.tools.erp_generator import generate_fixture
from invoiceops_agent.tools.erp_seed import seed_fixture
from invoiceops_agent.tools.ingestion_schemas import RawDocument

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


class FakeDocumentReader:
    async def read(
        self, reference: DocumentReference, *, run_id: UUID, trace_id: str
    ) -> RawDocument:
        return RawDocument(
            content_type=reference.content_type,
            content_hash=reference.content_hash,
            request_hash="0" * 64,
            body=b"%PDF-1.4 synthetic",
        )


class FakePreflight(DocumentPreflight):
    def __init__(self) -> None:
        pass

    async def check(self, document: RawDocument, *, run_id: UUID, trace_id: str) -> None:
        pass


class FakeGateway:
    def __init__(self, extraction: InvoiceExtraction) -> None:
        self.extraction = extraction
        self.completions = 0
        self.embeddings = 0

    def configured_policy(self, alias: str, context: RequestContext) -> AliasPolicy:
        return AliasPolicy(model_version="synthetic-extract@v1", allow_pdf=True)

    async def complete[T: BaseModel](
        self, request: GatewayRequest, response_model: type[T]
    ) -> GatewayResult[T]:
        self.completions += 1
        result: GatewayResult[InvoiceExtraction] = GatewayResult(
            value=self.extraction,
            provenance=GatewayProvenance(
                alias="extract-vision",
                model="synthetic-extract@v1",
                model_version="synthetic-extract@v1",
                prompt_version=request.prompt_version,
            ),
            usage=TokenUsage(input_tokens=10, output_tokens=10, total_tokens=20),
            attempts=1,
            latency_ms=1,
        )
        return cast(GatewayResult[T], result)

    async def embed(self, request: EmbeddingRequest) -> GatewayResult[EmbeddingValue]:
        self.embeddings += 1
        return GatewayResult(
            value=EmbeddingValue(vectors=((1.0, *(0.0 for _ in range(383))),)),
            provenance=GatewayProvenance(
                alias="embed",
                model="synthetic-embed@v1",
                model_version="synthetic-embed@v1",
                prompt_version=request.prompt_version,
            ),
            usage=TokenUsage(input_tokens=10, output_tokens=0, total_tokens=10),
            attempts=1,
            latency_ms=1,
        )


@pytest.mark.parametrize("auto_approval", [True, False])
async def test_full_graph_uses_real_erp_audit_and_replays_committed_extraction(
    ledger_runtime_dsn: str,
    auto_approval: bool,
) -> None:
    source = matching_request(snapshot_for("CLOSED"))
    assert source.snapshot is not None
    gateway = FakeGateway(source.extraction)
    ledger_writer = LedgerWriter(
        LedgerSettings(
            graph_version="invoice-v1",
            model_version="not-applicable@v1",
            prompt_version="not-applicable@v1",
            policy_version="not-applicable@v1",
        )
    )
    sink = TransactionalAuditSink(lambda: runtime_connection(ledger_runtime_dsn), ledger_writer)
    async with runtime_connection(ledger_runtime_dsn) as connection:
        async with connection.transaction():
            assert await seed_fixture(connection, generate_fixture()) == "created"
            await connection.execute(
                "UPDATE runs SET graph_version = 'invoice-v1' WHERE id = %s", (RUN_ID,)
            )
            await connection.execute(
                "UPDATE purchase_orders SET status = 'OPEN' WHERE po_number = %s",
                (source.snapshot.purchase_order.po_number,),
            )
    services = LiveInvoiceServices(
        connection=lambda: runtime_connection(ledger_runtime_dsn),
        extraction=ExtractionAgent(FakeDocumentReader(), FakePreflight(), gateway, sink),
        validation=ValidateNode(sink),
        matching=Match3WayNode(sink),
        similarity=NearDuplicateAgent(
            gateway, lambda: runtime_connection(ledger_runtime_dsn), ledger_writer
        ),
        taxonomy=ExceptionTaxonomyNode(sink),
        policy=PolicyNode(sink),
        audit=sink,
        audit_writer=ledger_writer,
        gate_config=CompositeGateConfig(auto_approval_enabled=auto_approval),
    )
    initial, version = await load_invoice_state(
        lambda: runtime_connection(ledger_runtime_dsn), RUN_ID, as_of=date(2026, 9, 23)
    )
    assert version == "invoice-v1"
    runner = InvoiceGraphRunner(
        build_invoice_graph(InMemorySaver(serde=restricted_serializer()), InvoiceNodes(services)),
        InProcessRunLock(),
    )

    async def run_once(run_id: UUID) -> InvoiceGraphState:
        assert run_id == RUN_ID
        return await runner.run(initial)

    result = await RetryWorker(
        PostgresAttemptStore(lambda: runtime_connection(ledger_runtime_dsn), RetryConfig()),
        run_once,
    ).process(RUN_ID)
    assert result.status == ("completed" if auto_approval else "awaiting_review")
    assert result.route == ("AUTO_APPROVE" if auto_approval else "REVIEW")
    assert gateway.completions == 1 and gateway.embeddings == 1
    assert await services.extract(initial) == await services.extract(initial)
    assert gateway.completions == 1
    with pytest.raises(ReplayEvidenceError, match="source changed"):
        await services.extract(initial.model_copy(update={"content_hash": "f" * 64}))
    assert await runner.run(initial) == result
    async with runtime_connection(ledger_runtime_dsn) as connection:
        page = await LedgerReader().for_run(connection, RUN_ID, trace_id=initial.trace_id)
        status = await (
            await connection.execute("SELECT status FROM invoices WHERE id = %s", (INVOICE_ID,))
        ).fetchone()
        run_status = await (
            await connection.execute("SELECT status FROM runs WHERE id = %s", (RUN_ID,))
        ).fetchone()
        exception = await (
            await connection.execute(
                "SELECT exception_type, status, priority, sla_due_at, evidence, recommendation "
                "FROM exceptions WHERE run_id = %s",
                (RUN_ID,),
            )
        ).fetchone()
    assert status == {"status": "APPROVED" if auto_approval else "NEEDS_REVIEW"}
    assert run_status == {"status": "COMPLETED" if auto_approval else "PAUSED"}
    assert [event.event_type for event in page.events] == [
        "extraction.completed",
        "validation.completed",
        "matching.completed",
        "similarity.completed",
        "classification.completed",
        "policy.completed",
        "gate.completed",
        *(["approval.auto_granted", "workflow.archived"] if auto_approval else ["triage.prepared"]),
    ]
    gate_event = next(event for event in page.events if event.event_type == "gate.completed")
    audited_gate = CompositeGateResult.model_validate(gate_event.payload)
    assert gate_event.versions.policy_version == "composite-gate@v1"
    assert audited_gate.score == 1
    assert audited_gate.route == ("AUTO_APPROVE" if auto_approval else "REVIEW")
    if not auto_approval:
        assert exception is not None
        assert exception["status"] == "OPEN"
        assert exception["priority"] == 1
        assert exception["evidence"]["extraction_escalated"] is False
        assert exception["recommendation"]["recommendation"] == "REVIEW"
        assert page.events[-1].versions.policy_version == "exception-queue@v1"
        return
    assert exception is None
    transitions = WorkflowTransitions(lambda: runtime_connection(ledger_runtime_dsn), ledger_writer)
    await transitions.append_once(
        result,
        event_type="workflow.archived",
        node="Archive",
        actor_type="SYSTEM",
        actor_id="invoiceops-workflow",
        payload={"route": "AUTO_APPROVE"},
    )
    with pytest.raises(ReplayEvidenceError, match="differs"):
        await transitions.append_once(
            result,
            event_type="workflow.archived",
            node="Archive",
            actor_type="SYSTEM",
            actor_id="invoiceops-workflow",
            payload={"route": "REVIEW"},
        )
