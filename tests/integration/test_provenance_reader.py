"""Provenance pages over real append-only ledger and restricted runtime role."""

from uuid import UUID

import pytest
from pydantic import SecretStr
from tests.integration.support import INVOICE_ID, RUN_ID
from tests.integration.test_ledger import ledger_runtime_dsn as ledger_runtime_dsn
from tests.integration.test_ledger import runtime_connection, writer

from invoiceops_agent.api.provenance_reader import (
    PostgresProvenanceReader,
    ProvenanceNotFound,
)
from invoiceops_agent.api.settings import ApiSettings
from invoiceops_agent.ledger.schemas import AppendEvent

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
TRACE_ID = "b" * 32


async def test_run_trace_excludes_payload_and_pages_committed_events(
    ledger_runtime_dsn: str,
) -> None:
    async with runtime_connection(ledger_runtime_dsn) as connection, connection.transaction():
        for sequence in range(2):
            await writer(event_id=UUID(int=40 + sequence)).append(
                connection,
                AppendEvent(
                    run_id=RUN_ID,
                    invoice_id=INVOICE_ID,
                    event_type=f"synthetic.trace.{sequence}",
                    node="Validate",
                    actor_type="POLICY",
                    actor_id="synthetic-policy",
                    payload={"private_evidence": "synthetic-secret"},
                ),
                trace_id=TRACE_ID,
            )
    reader = PostgresProvenanceReader(ApiSettings(postgres_dsn=SecretStr(ledger_runtime_dsn)))
    first = await reader.for_run_trace(RUN_ID, trace_id=TRACE_ID, limit=1)
    assert first.run_id == RUN_ID
    assert first.invoice_id == INVOICE_ID
    assert first.trace_id == "0" * 32
    assert first.events[0].id == UUID(int=40)
    assert first.events[0].versions.policy_version == "not-applicable@v1"
    assert "private_evidence" not in first.model_dump_json()
    assert first.next_cursor is not None
    second = await reader.for_run_trace(
        RUN_ID, trace_id=TRACE_ID, limit=1, after_sequence=first.next_cursor.sequence
    )
    assert [event.id for event in second.events] == [UUID(int=41)]
    assert second.next_cursor is None
    with pytest.raises(ProvenanceNotFound):
        await reader.for_run_trace(UUID(int=999), trace_id=TRACE_ID)


async def test_invoice_provenance_crosses_runs_with_stable_tied_timestamp_cursor(
    ledger_runtime_dsn: str,
) -> None:
    next_run = UUID(int=101)
    async with runtime_connection(ledger_runtime_dsn) as connection, connection.transaction():
        await connection.execute(
            "INSERT INTO public.runs (id, invoice_id, graph_version, trace_id) "
            "VALUES (%s, %s, 'graph@v1', %s)",
            (next_run, INVOICE_ID, TRACE_ID),
        )
        for run_id, event_id in ((RUN_ID, UUID(int=40)), (next_run, UUID(int=41))):
            await writer(event_id=event_id).append(
                connection,
                AppendEvent(
                    run_id=run_id,
                    invoice_id=INVOICE_ID,
                    event_type="synthetic.provenance",
                    node="HumanReview",
                    actor_type="HUMAN",
                    actor_id="synthetic-auditor",
                    payload={"reason": "synthetic decision"},
                ),
                trace_id=TRACE_ID,
            )
    reader = PostgresProvenanceReader(ApiSettings(postgres_dsn=SecretStr(ledger_runtime_dsn)))
    first = await reader.for_invoice(INVOICE_ID, trace_id=TRACE_ID, limit=1)
    assert first.invoice_id == INVOICE_ID
    assert first.events[0].id == UUID(int=40)
    assert first.events[0].payload == {"reason": "synthetic decision"}
    assert first.next_cursor is not None
    second = await reader.for_invoice(
        INVOICE_ID,
        trace_id=TRACE_ID,
        limit=1,
        after_created_at=first.next_cursor.created_at,
        after_event_id=first.next_cursor.id,
    )
    assert [event.id for event in second.events] == [UUID(int=41)]
    assert second.events[0].run_id == next_run
    assert second.next_cursor is None
    with pytest.raises(ProvenanceNotFound):
        await reader.for_invoice(UUID(int=999), trace_id=TRACE_ID)
