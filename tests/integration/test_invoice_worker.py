"""Restricted-role retry state, terminal audit, and idempotent dead-letter redrive."""

from datetime import UTC, date, datetime

import pytest
from tests.integration.support import INVOICE_ID, RUN_ID
from tests.integration.test_ledger import ledger_runtime_dsn as ledger_runtime_dsn
from tests.integration.test_ledger import runtime_connection

from invoiceops_agent.graph.retry import RetryConfig
from invoiceops_agent.graph.state import InvoiceGraphState
from invoiceops_agent.graph.worker import InvalidRedrive, PostgresAttemptStore, RunInDeadLetter
from invoiceops_agent.ledger.reader import LedgerReader

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
NOW = datetime(2026, 9, 24, 12, tzinfo=UTC)


async def test_worker_dead_letter_and_idempotent_redrive(ledger_runtime_dsn: str) -> None:
    store = PostgresAttemptStore(
        lambda: runtime_connection(ledger_runtime_dsn), RetryConfig(), clock=lambda: NOW
    )
    first = await store.begin(RUN_ID)
    assert first is not None and first.attempt == 1
    await store.failed(first, error_type="OperationalError", retryable=True, terminal=False)
    second = await store.begin(RUN_ID, continuation=True)
    assert second is not None and second.attempt == 2
    await store.failed(second, error_type="ValueError", retryable=False, terminal=True)
    await store.failed(second, error_type="ValueError", retryable=False, terminal=True)

    dead_letters = await store.list_dead_letters()
    assert len(dead_letters) == 1
    assert dead_letters[0].run_id == RUN_ID
    assert dead_letters[0].attempts == 2
    assert dead_letters[0].error_type == "ValueError"
    assert not dead_letters[0].retryable
    with pytest.raises(RunInDeadLetter):
        await store.begin(RUN_ID)
    with pytest.raises(InvalidRedrive):
        await store.redrive(
            RUN_ID, key="bad key", actor_id="synthetic-reviewer", reason="Synthetic recovery"
        )

    assert await store.redrive(
        RUN_ID, key="retry-1", actor_id="synthetic-reviewer", reason="Synthetic recovery"
    )
    assert not await store.redrive(
        RUN_ID, key="retry-1", actor_id="synthetic-reviewer", reason="Synthetic recovery"
    )
    with pytest.raises(InvalidRedrive, match="another request"):
        await store.redrive(
            RUN_ID, key="retry-1", actor_id="other-reviewer", reason="Synthetic recovery"
        )
    assert await store.list_dead_letters() == []
    next_attempt = await store.begin(RUN_ID)
    assert next_attempt is not None and next_attempt.attempt == 1 and next_attempt.cycle == 1
    settled = InvoiceGraphState(
        run_id=RUN_ID,
        invoice_id=INVOICE_ID,
        trace_id="0" * 32,
        as_of=date(2026, 9, 24),
        raw_ref="sha256/aa/" + "a" * 64,
        content_hash="a" * 64,
        content_type="application/pdf",
        status="awaiting_review",
        route="REVIEW",
        completed_nodes=["Ingest", "ExceptionTriage"],
    )
    await store.settle(next_attempt, settled)
    await store.settle(next_attempt, settled)
    async with runtime_connection(ledger_runtime_dsn) as connection:
        row = await (
            await connection.execute("SELECT status, error FROM runs WHERE id = %s", (RUN_ID,))
        ).fetchone()
        events = await LedgerReader().for_run(connection, RUN_ID, trace_id="0" * 32)
    assert row == {"status": "PAUSED", "error": None}
    assert [event.event_type for event in events.events] == [
        "workflow.dead_lettered",
        "workflow.redriven",
    ]
    assert events.events[0].payload == {
        "attempts": 2,
        "cycle": 0,
        "error_type": "ValueError",
        "retryable": False,
    }
