"""ADK invoice routes, session replay, and structured human review without network I/O."""

from uuid import UUID

import pytest
from google.adk.sessions import InMemorySessionService
from pydantic import SecretStr
from tests.unit.test_invoice_graph import INVOICE_ID, RUN_ID, TRACE_ID, FakeServices, _state

from invoiceops_agent.agents.near_duplicate_settings import LiteLLMWorkflowSettings
from invoiceops_agent.graph.adk_invoice import build_adk_invoice_workflow
from invoiceops_agent.graph.adk_runner import AdkInvoiceRunner
from invoiceops_agent.graph.checkpoints import InProcessRunLock
from invoiceops_agent.graph.errors import InvalidCheckpoint, RunConflict
from invoiceops_agent.graph.invoice_nodes import InvoiceNodes
from invoiceops_agent.graph.state import ReviewDecision

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def _sessions() -> InMemorySessionService:
    return InMemorySessionService()  # type: ignore[no-untyped-call]


async def test_adk_model_uses_only_a_named_gateway_route() -> None:
    settings = LiteLLMWorkflowSettings(
        api_base="https://gateway.synthetic.example/v1",
        master_key=SecretStr("synthetic-key"),
        model="base-alias",
        embed_model="embedding-alias",
        adk_model="gemini-alias",
        _env_file=None,
    )
    gateway = settings.adk_gateway_settings()
    assert gateway.aliases["extract-vision"].routes_for("restricted", "extract-vision") == (
        ("gemini-alias", "gemini-alias"),
    )
    assert gateway.aliases["triage-reasoner"].routes_for("restricted", "triage-reasoner") == (
        ("gemini-alias", "gemini-alias"),
    )
    assert gateway.aliases["embed"].routes_for("restricted", "embed") == (
        ("embedding-alias", "embedding-alias"),
    )
    with pytest.raises(ValueError, match="LITELLM_ADK_MODEL"):
        settings.model_copy(update={"adk_model": None}).adk_gateway_settings()


def _runner(sessions: InMemorySessionService, services: FakeServices) -> AdkInvoiceRunner:
    return AdkInvoiceRunner(
        build_adk_invoice_workflow(InvoiceNodes(services)), sessions, InProcessRunLock()
    )


async def test_adk_auto_path_and_replay() -> None:
    sessions = _sessions()
    services = FakeServices(auto_enabled=True)
    runner = _runner(sessions, services)
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
    assert await _runner(sessions, FakeServices()).run(_state()) == result
    assert services.calls == result.completed_nodes[1:]
    with pytest.raises(RunConflict):
        await runner.run(_state().model_copy(update={"invoice_id": UUID(int=812)}))


async def test_adk_review_pauses_and_resumes_once() -> None:
    sessions = _sessions()
    services = FakeServices()
    runner = _runner(sessions, services)
    paused = await runner.run(_state())
    assert paused.status == "awaiting_review"
    assert paused.completed_nodes[-1] == "ExceptionTriage"
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
    assert services.calls.count("HumanReview") == 1
    with pytest.raises(InvalidCheckpoint):
        await runner.resume(
            run_id=RUN_ID,
            invoice_id=INVOICE_ID,
            trace_id=TRACE_ID,
            decision=decision.model_copy(update={"action": "ESCALATE"}),
        )


async def test_adk_duplicate_and_extraction_failure_routes() -> None:
    duplicate = FakeServices()
    rejected = await _runner(_sessions(), duplicate).run(_state(duplicate=True))
    assert rejected.status == "rejected"
    assert rejected.completed_nodes == ["Ingest", "Reject"]
    assert duplicate.calls == ["Reject"]
    failed = FakeServices(extraction_fails=True)
    paused = await _runner(_sessions(), failed).run(_state())
    assert paused.status == "awaiting_review"
    assert paused.completed_nodes == ["Ingest", "Extract", "ExceptionTriage"]
