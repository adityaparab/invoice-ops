"""Run progress reads committed ledger observations from the restricted runtime role."""

from uuid import UUID

import pytest
from pydantic import SecretStr
from tests.integration.support import INVOICE_ID, RUN_ID
from tests.integration.test_ledger import ledger_runtime_dsn as ledger_runtime_dsn
from tests.integration.test_ledger import runtime_connection, writer

from invoiceops_agent.api.run_progress_reader import PostgresRunProgressReader, RunNotFound
from invoiceops_agent.api.settings import ApiSettings
from invoiceops_agent.ledger.schemas import AppendEvent

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_run_progress_reads_bounded_audited_state(ledger_runtime_dsn: str) -> None:
    async with runtime_connection(ledger_runtime_dsn) as connection, connection.transaction():
        await connection.execute(
            "UPDATE public.runs SET status = 'RUNNING' WHERE id = %s", (RUN_ID,)
        )
        await writer().append(
            connection,
            AppendEvent(
                run_id=RUN_ID,
                invoice_id=INVOICE_ID,
                event_type="ingest.accepted",
                node="Ingest",
                actor_type="SYSTEM",
                actor_id="synthetic-ingestion",
                payload={"source": "UPLOAD", "raw_ref": "secret-object-key"},
            ),
            trace_id="a" * 32,
        )
    reader = PostgresRunProgressReader(ApiSettings(postgres_dsn=SecretStr(ledger_runtime_dsn)))
    progress = await reader.read(RUN_ID)
    assert progress.status == "RUNNING"
    assert progress.active_node == "Extract"
    assert progress.nodes[0].event_type == "ingest.accepted"
    assert progress.nodes[0].state == {"source": "UPLOAD"}
    assert all(node.observed_at is None for node in progress.nodes[1:])
    assert "secret-object-key" not in progress.model_dump_json()
    with pytest.raises(RunNotFound):
        await reader.read(UUID(int=999))


async def test_run_progress_uses_latest_observation_for_a_retried_node(
    ledger_runtime_dsn: str,
) -> None:
    async with runtime_connection(ledger_runtime_dsn) as connection, connection.transaction():
        await connection.execute(
            "UPDATE public.runs SET status = 'RUNNING' WHERE id = %s", (RUN_ID,)
        )
        for event_type, status in (
            ("extraction.completed", "EXTRACTED"),
            ("extraction.escalated", "ESCALATED"),
        ):
            await writer().append(
                connection,
                AppendEvent(
                    run_id=RUN_ID,
                    invoice_id=INVOICE_ID,
                    event_type=event_type,
                    node="Extract",
                    actor_type="AGENT",
                    actor_id="synthetic-extractor",
                    payload={"result": {"status": status}},
                ),
                trace_id="a" * 32,
            )
    progress = await PostgresRunProgressReader(
        ApiSettings(postgres_dsn=SecretStr(ledger_runtime_dsn))
    ).read(RUN_ID)
    assert progress.nodes[1].event_type == "extraction.escalated"
    assert progress.nodes[1].state == {"status": "ESCALATED"}
    assert progress.active_node == "ExceptionTriage"
