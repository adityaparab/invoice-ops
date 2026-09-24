"""Signed decisions settle the paused run only after committed graph evidence."""

from datetime import UTC, date, datetime
from uuid import UUID

import pytest
from pydantic import SecretStr
from tests.integration.support import INVOICE_ID, RUN_ID
from tests.integration.test_decision_api import EXCEPTION_ID, _seed_review
from tests.integration.test_ledger import ledger_runtime_dsn as ledger_runtime_dsn
from tests.integration.test_ledger import runtime_connection

from invoiceops_agent.api.decision_service import DecisionService
from invoiceops_agent.api.schemas.decision import DecisionRequest
from invoiceops_agent.api.settings import ApiSettings
from invoiceops_agent.graph.review_worker import DecisionResumeWorker, ReviewWorkerError
from invoiceops_agent.graph.runtime import InvoiceRuntimeSettings, load_invoice_state
from invoiceops_agent.graph.state import InvoiceGraphState, ReviewDecision
from invoiceops_agent.ledger.schemas import AppendEvent
from invoiceops_agent.ledger.settings import LedgerSettings
from invoiceops_agent.ledger.writer import LedgerWriter

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_review_worker_resumes_signed_decision_once(ledger_runtime_dsn: str) -> None:
    await _seed_review(ledger_runtime_dsn)
    async with runtime_connection(ledger_runtime_dsn) as connection, connection.transaction():
        await connection.execute(
            "UPDATE public.runs SET graph_version = 'invoice-v1' WHERE id = %s", (RUN_ID,)
        )
    service = DecisionService(ApiSettings(postgres_dsn=SecretStr(ledger_runtime_dsn)))
    proposal = await service.submit(
        exception_id=EXCEPTION_ID,
        request=DecisionRequest(
            action="RETURN", rationale="Synthetic mismatch", reason_code="PRICE_MM"
        ),
        role="ANALYST",
        idempotency_key="review-proposal",
        trace_id="a" * 32,
    )
    accepted = await service.submit(
        exception_id=EXCEPTION_ID,
        request=DecisionRequest(
            action="RETURN",
            rationale="Manager verified",
            reason_code="PRICE_MM",
            proposal_id=proposal.decision_id,
        ),
        role="MANAGER",
        idempotency_key="review-signoff",
        trace_id="a" * 32,
    )
    calls: list[UUID] = []

    async def fake_resume(run_id: UUID, decision: ReviewDecision) -> InvoiceGraphState:
        calls.append(run_id)
        ledger_writer = LedgerWriter(
            LedgerSettings(
                graph_version="invoice-v1",
                model_version="not-applicable@v1",
                prompt_version="not-applicable@v1",
                policy_version="not-applicable@v1",
            )
        )
        async with runtime_connection(ledger_runtime_dsn) as connection, connection.transaction():
            await ledger_writer.append(
                connection,
                AppendEvent(
                    run_id=RUN_ID,
                    invoice_id=INVOICE_ID,
                    event_type="review.recorded",
                    node="HumanReview",
                    actor_type="HUMAN",
                    actor_id=decision.actor_id,
                    payload=decision.model_dump(mode="json"),
                ),
                trace_id="a" * 32,
            )
            await ledger_writer.append(
                connection,
                AppendEvent(
                    run_id=RUN_ID,
                    invoice_id=INVOICE_ID,
                    event_type="workflow.archived",
                    node="Archive",
                    actor_type="SYSTEM",
                    actor_id="invoiceops-workflow",
                    payload={"route": "REVIEW"},
                ),
                trace_id="a" * 32,
            )
            await connection.execute(
                "UPDATE public.invoices SET status = 'RETURNED' WHERE id = %s", (INVOICE_ID,)
            )
        initial, _ = await load_invoice_state(
            lambda: runtime_connection(ledger_runtime_dsn), RUN_ID, as_of=date(2026, 9, 24)
        )
        return InvoiceGraphState.model_validate(
            {
                **initial.model_dump(mode="json"),
                "status": "completed",
                "route": "REVIEW",
                "review": decision.model_dump(mode="json"),
                "completed_nodes": ["Ingest", "HumanReview", "Archive"],
            }
        )

    worker = DecisionResumeWorker(
        InvoiceRuntimeSettings(postgres_dsn=SecretStr(ledger_runtime_dsn), _env_file=None),
        resume=fake_resume,
        clock=lambda: datetime(2026, 9, 24, 12, tzinfo=UTC),
    )
    with pytest.raises(ReviewWorkerError, match="independent signoff"):
        await worker.process(proposal.decision_id)
    result = await worker.process(accepted.decision_id)
    assert result == {
        "decision_id": str(accepted.decision_id),
        "run_id": str(RUN_ID),
        "status": "RESOLVED",
    }
    assert await worker.process(accepted.decision_id) == result
    assert calls == [RUN_ID]
    async with runtime_connection(ledger_runtime_dsn) as connection:
        row = await (
            await connection.execute(
                "SELECT r.status AS run_status, r.completed_at, e.status AS exception_status, "
                "i.status AS invoice_status FROM public.runs r "
                "JOIN public.exceptions e ON e.run_id = r.id "
                "JOIN public.invoices i ON i.id = r.invoice_id WHERE r.id = %s",
                (RUN_ID,),
            )
        ).fetchone()
    assert row is not None
    assert row["run_status"] == "COMPLETED"
    assert row["completed_at"] == datetime(2026, 9, 24, 12, tzinfo=UTC)
    assert row["exception_status"] == "RESOLVED"
    assert row["invoice_status"] == "RETURNED"


async def test_worker_keeps_run_paused_without_committed_graph_evidence(
    ledger_runtime_dsn: str,
) -> None:
    await _seed_review(ledger_runtime_dsn)
    async with runtime_connection(ledger_runtime_dsn) as connection, connection.transaction():
        await connection.execute(
            "UPDATE public.runs SET graph_version = 'invoice-v1' WHERE id = %s", (RUN_ID,)
        )
    service = DecisionService(ApiSettings(postgres_dsn=SecretStr(ledger_runtime_dsn)))
    proposal = await service.submit(
        exception_id=EXCEPTION_ID,
        request=DecisionRequest(
            action="ESCALATE", rationale="Needs review", reason_code="ESCALATE"
        ),
        role="ANALYST",
        idempotency_key="incomplete-proposal",
        trace_id="a" * 32,
    )
    accepted = await service.submit(
        exception_id=EXCEPTION_ID,
        request=DecisionRequest(
            action="ESCALATE",
            rationale="Manager agrees",
            reason_code="ESCALATE",
            proposal_id=proposal.decision_id,
        ),
        role="MANAGER",
        idempotency_key="incomplete-signoff",
        trace_id="a" * 32,
    )

    async def no_evidence(run_id: UUID, decision: ReviewDecision) -> InvoiceGraphState:
        initial, _ = await load_invoice_state(
            lambda: runtime_connection(ledger_runtime_dsn), run_id, as_of=date(2026, 9, 24)
        )
        return InvoiceGraphState.model_validate(
            {
                **initial.model_dump(mode="json"),
                "status": "completed",
                "route": "REVIEW",
                "review": decision.model_dump(mode="json"),
                "completed_nodes": ["Ingest", "HumanReview", "Archive"],
            }
        )

    worker = DecisionResumeWorker(
        InvoiceRuntimeSettings(postgres_dsn=SecretStr(ledger_runtime_dsn), _env_file=None),
        resume=no_evidence,
    )
    with pytest.raises(ReviewWorkerError, match="evidence is incomplete"):
        await worker.process(accepted.decision_id)
    async with runtime_connection(ledger_runtime_dsn) as connection:
        row = await (
            await connection.execute(
                "SELECT r.status AS run_status, e.status AS exception_status FROM public.runs r "
                "JOIN public.exceptions e ON e.run_id = r.id WHERE r.id = %s",
                (RUN_ID,),
            )
        ).fetchone()
    assert row == {"run_status": "PAUSED", "exception_status": "IN_REVIEW"}
