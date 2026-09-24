"""The auditor read adapter pages immutable ledger records with the runtime role."""

from uuid import UUID

import pytest
from pydantic import SecretStr
from tests.integration.support import INVOICE_ID, RUN_ID
from tests.integration.test_ledger import ledger_runtime_dsn as ledger_runtime_dsn
from tests.integration.test_ledger import runtime_connection, writer

from invoiceops_agent.api.audit_reader import AuditRunNotFound, PostgresAuditReader
from invoiceops_agent.api.settings import ApiSettings
from invoiceops_agent.ledger.schemas import AppendEvent

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_run_ledger_pages_include_actor_versions_and_trace(ledger_runtime_dsn: str) -> None:
    async with runtime_connection(ledger_runtime_dsn) as connection, connection.transaction():
        for sequence in range(2):
            await writer().append(
                connection,
                AppendEvent(
                    run_id=RUN_ID,
                    invoice_id=INVOICE_ID,
                    event_type=f"synthetic.audit.{sequence}",
                    node="Validate",
                    actor_type="POLICY",
                    actor_id="synthetic-audit-policy",
                    payload={"sequence": sequence},
                ),
                trace_id="a" * 32,
            )
    reader = PostgresAuditReader(ApiSettings(postgres_dsn=SecretStr(ledger_runtime_dsn)))
    first = await reader.for_run(RUN_ID, trace_id="b" * 32, limit=1)
    assert first.run_id == RUN_ID
    assert first.invoice_id == INVOICE_ID
    assert first.trace_id == "0" * 32
    assert len(first.events) == 1
    assert first.events[0].actor_id == "synthetic-audit-policy"
    assert first.events[0].versions.policy_version
    assert first.next_cursor is not None
    second = await reader.for_run(
        RUN_ID, trace_id="b" * 32, limit=1, after_sequence=first.next_cursor.sequence
    )
    assert len(second.events) == 1
    assert second.events[0].sequence > first.events[0].sequence
    assert second.next_cursor is None
    with pytest.raises(AuditRunNotFound):
        await reader.for_run(UUID(int=999), trace_id="b" * 32)
