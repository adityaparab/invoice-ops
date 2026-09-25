"""ADK's Postgres session preserves the human review pause across runner restarts."""

import pytest
from tests.integration.test_graph import checkpoint_settings as checkpoint_settings
from tests.unit.test_invoice_graph import FakeServices, _state

from invoiceops_agent.graph.checkpoints import postgres_adk_invoice_graph
from invoiceops_agent.graph.invoice_nodes import InvoiceNodes
from invoiceops_agent.graph.settings import GraphSettings
from invoiceops_agent.graph.state import ReviewDecision

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_adk_review_resumes_after_postgres_session_restart(
    checkpoint_settings: GraphSettings,
) -> None:
    first_services = FakeServices()
    async with postgres_adk_invoice_graph(
        checkpoint_settings, InvoiceNodes(first_services)
    ) as runner:
        paused = await runner.run(_state())
        assert paused.status == "awaiting_review"
    restarted_services = FakeServices()
    async with postgres_adk_invoice_graph(
        checkpoint_settings, InvoiceNodes(restarted_services)
    ) as restarted:
        assert await restarted.run(_state()) == paused
        decision = ReviewDecision(
            action="RETURN",
            actor_id="synthetic-reviewer",
            rationale="Synthetic mismatch",
            reason_code="REVIEW",
        )
        completed = await restarted.resume(
            run_id=paused.run_id,
            invoice_id=paused.invoice_id,
            trace_id=paused.trace_id,
            decision=decision,
        )
        assert completed.status == "completed"
        assert completed.review == decision.model_dump(mode="json")
    assert first_services.calls[-1] == "ExceptionTriage"
    assert restarted_services.calls == ["HumanReview", "Archive"]
