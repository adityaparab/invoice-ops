"""Invoice graph branching, checkpoints, replay, and human resume without network I/O."""

from datetime import date
from uuid import UUID

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import JsonValue
from tests.unit.policy_support import policy_request

from invoiceops_agent.graph.checkpoints import InProcessRunLock, restricted_serializer
from invoiceops_agent.graph.errors import RunConflict
from invoiceops_agent.graph.invoice import build_invoice_graph
from invoiceops_agent.graph.invoice_nodes import InvoiceNodes
from invoiceops_agent.graph.invoice_runner import InvoiceGraphRunner
from invoiceops_agent.graph.state import InvoiceGraphState, ReviewDecision
from invoiceops_agent.schemas.exceptions import TaxonomyResult
from invoiceops_agent.schemas.extraction import (
    ExtractionEscalation,
    ExtractionResult,
    ExtractionSuccess,
)
from invoiceops_agent.schemas.gate import GateConfig, GateResult
from invoiceops_agent.schemas.matching import ERPSnapshot, MatchResult
from invoiceops_agent.schemas.policy import PolicyResult
from invoiceops_agent.schemas.similarity import SimilarityResult
from invoiceops_agent.schemas.validation import ValidationResult
from invoiceops_agent.tools.gate import evaluate_provisional_gate

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]
RUN_ID = UUID(int=810)
INVOICE_ID = UUID(int=811)
TRACE_ID = "e" * 32


class FakeServices:
    def __init__(self, *, auto_enabled: bool = False, extraction_fails: bool = False) -> None:
        self.calls: list[str] = []
        self.auto_enabled = auto_enabled
        self.extraction_fails = extraction_fails
        self.evidence = policy_request()

    async def extract(self, state: InvoiceGraphState) -> ExtractionResult:
        self.calls.append("Extract")
        if self.extraction_fails:
            return ExtractionEscalation(
                run_id=state.run_id,
                invoice_id=state.invoice_id,
                trace_id=state.trace_id,
                calls=(),
                reason="INVALID_DOCUMENT",
            )
        return ExtractionSuccess(
            run_id=state.run_id,
            invoice_id=state.invoice_id,
            trace_id=state.trace_id,
            calls=(),
            extraction=self.evidence.extraction,
        )

    async def validate(self, state: InvoiceGraphState) -> ValidationResult:
        self.calls.append("Validate")
        return self.evidence.validation

    async def match(self, state: InvoiceGraphState) -> tuple[ERPSnapshot | None, MatchResult]:
        self.calls.append("Match3Way")
        return self.evidence.snapshot, self.evidence.match

    async def policy(
        self, state: InvoiceGraphState
    ) -> tuple[SimilarityResult | None, TaxonomyResult, PolicyResult]:
        self.calls.append("Policy")
        return None, self.evidence.taxonomy, self.evidence_policy()

    def evidence_policy(self) -> PolicyResult:
        from invoiceops_agent.tools.policy import evaluate_policy

        return evaluate_policy(self.evidence)

    async def gate(self, state: InvoiceGraphState) -> GateResult:
        self.calls.append("Gate")
        return evaluate_provisional_gate(
            self.evidence.extraction,
            self.evidence_policy(),
            GateConfig(auto_approval_enabled=self.auto_enabled),
        )

    async def triage(self, state: InvoiceGraphState) -> dict[str, JsonValue]:
        self.calls.append("ExceptionTriage")
        return {"recommendation": "REVIEW"}

    async def auto_approve(self, state: InvoiceGraphState) -> None:
        self.calls.append("AutoApprove")

    async def review(self, state: InvoiceGraphState, decision: ReviewDecision) -> None:
        self.calls.append("HumanReview")

    async def archive(self, state: InvoiceGraphState) -> None:
        self.calls.append("Archive")

    async def reject(self, state: InvoiceGraphState) -> None:
        self.calls.append("Reject")


def _state(*, duplicate: bool = False) -> InvoiceGraphState:
    return InvoiceGraphState(
        run_id=RUN_ID,
        invoice_id=INVOICE_ID,
        trace_id=TRACE_ID,
        as_of=date(2026, 9, 23),
        raw_ref="sha256/aa/" + "a" * 64,
        content_hash="a" * 64,
        content_type="application/pdf",
        duplicate=duplicate,
    )


def _runner(saver: InMemorySaver, services: FakeServices) -> InvoiceGraphRunner:
    return InvoiceGraphRunner(
        build_invoice_graph(saver, InvoiceNodes(services)),
        InProcessRunLock(),
    )


async def test_workflow_node_and_tool_spans_share_a_trace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(trace, "get_tracer", provider.get_tracer)
    result = await _runner(
        InMemorySaver(serde=restricted_serializer()), FakeServices(auto_enabled=True)
    ).run(_state())

    spans = exporter.get_finished_spans()
    names = {span.name for span in spans}
    assert {f"invoiceops.node.{name}" for name in result.completed_nodes} <= names
    assert {"invoiceops.tool.extract", "invoiceops.tool.match", "invoiceops.tool.gate"} <= names
    workflow = next(span for span in spans if span.name == "invoiceops.workflow.invoice")
    extract = next(span for span in spans if span.name == "invoiceops.node.Extract")
    tool = next(span for span in spans if span.name == "invoiceops.tool.extract")
    assert workflow.context is not None
    assert extract.context is not None
    assert extract.parent is not None
    assert tool.parent is not None
    assert extract.parent.span_id == workflow.context.span_id
    assert tool.parent.span_id == extract.context.span_id
    assert len({span.context.trace_id for span in spans if span.context is not None}) == 1
    provider.shutdown()


async def test_auto_path_checkpoints_every_node_and_replays_without_effects() -> None:
    saver = InMemorySaver(serde=restricted_serializer())
    services = FakeServices(auto_enabled=True)
    runner = _runner(saver, services)
    result = await runner.run(_state())
    assert result.status == "completed"
    assert result.route == "AUTO_APPROVE"
    assert result.completed_nodes == [
        "Ingest",
        "Extract",
        "Validate",
        "Match3Way",
        "Policy",
        "Gate",
        "AutoApprove",
        "Archive",
    ]
    assert services.calls == result.completed_nodes[1:]
    snapshots = [
        item
        async for item in runner.graph.aget_state_history(
            {"configurable": {"thread_id": str(RUN_ID)}}
        )
    ]
    assert len(snapshots) >= len(result.completed_nodes) + 1
    assert await _runner(saver, FakeServices()).run(_state()) == result
    assert services.calls == result.completed_nodes[1:]
    with pytest.raises(RunConflict):
        await runner.run(_state().model_copy(update={"invoice_id": UUID(int=812)}))


async def test_review_pauses_and_resumes_only_after_human_decision() -> None:
    saver = InMemorySaver(serde=restricted_serializer())
    services = FakeServices()
    runner = _runner(saver, services)
    paused = await runner.run(_state())
    assert paused.status == "awaiting_review"
    assert paused.completed_nodes[-1] == "ExceptionTriage"
    assert services.calls[-1] == "ExceptionTriage"
    assert await runner.run(_state()) == paused
    decision = ReviewDecision(
        action="RETURN",
        actor_id="synthetic-reviewer",
        rationale="Synthetic mismatch",
        reason_code="REVIEW",
    )
    completed = await runner.resume(
        run_id=RUN_ID, invoice_id=INVOICE_ID, trace_id=TRACE_ID, decision=decision
    )
    assert completed.status == "completed"
    assert completed.review == decision.model_dump(mode="json")
    assert services.calls[-2:] == ["HumanReview", "Archive"]
    assert (
        await runner.resume(
            run_id=RUN_ID, invoice_id=INVOICE_ID, trace_id=TRACE_ID, decision=decision
        )
        == completed
    )
    assert services.calls[-2:] == ["HumanReview", "Archive"]


async def test_duplicate_and_failed_extraction_branches() -> None:
    duplicate = FakeServices()
    rejected = await _runner(InMemorySaver(serde=restricted_serializer()), duplicate).run(
        _state(duplicate=True)
    )
    assert rejected.status == "rejected"
    assert rejected.completed_nodes == ["Ingest", "Reject"]
    assert duplicate.calls == ["Reject"]
    failed = FakeServices(extraction_fails=True)
    paused = await _runner(InMemorySaver(serde=restricted_serializer()), failed).run(_state())
    assert paused.status == "awaiting_review"
    assert paused.completed_nodes == ["Ingest", "Extract", "ExceptionTriage"]
    assert failed.calls == ["Extract", "ExceptionTriage"]
