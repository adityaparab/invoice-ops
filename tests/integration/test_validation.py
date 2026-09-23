"""Real restricted-role validation decisions commit atomically through the shared audit sink."""

import pytest
from tests.integration.support import INVOICE_ID, RUN_ID
from tests.integration.test_ledger import (
    ledger_runtime_dsn as ledger_runtime_dsn,
)
from tests.integration.test_ledger import runtime_connection, writer
from tests.unit.test_validation import invoice, strict_usd

from invoiceops_agent.graph.nodes.validate import ValidateNode
from invoiceops_agent.ledger.audit import TransactionalAuditSink
from invoiceops_agent.ledger.connection import LedgerConnection
from invoiceops_agent.ledger.errors import LedgerStorageError
from invoiceops_agent.ledger.reader import LedgerReader
from invoiceops_agent.ledger.schemas import AppendEvent, LedgerEvent
from invoiceops_agent.schemas.validation import ValidationRequest, ValidationResult

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
TRACE_ID = "c" * 32


@pytest.mark.parametrize("valid", [True, False])
async def test_policy_decision_and_decimal_evidence_are_committed_before_return(
    ledger_runtime_dsn: str,
    valid: bool,
) -> None:
    sink = TransactionalAuditSink(lambda: runtime_connection(ledger_runtime_dsn), writer())
    request = ValidationRequest(
        run_id=RUN_ID,
        invoice_id=INVOICE_ID,
        trace_id=TRACE_ID,
        extraction=invoice(total_amount="24" if valid else "99"),
    )
    result = await ValidateNode(sink, strict_usd()).run(request)
    async with runtime_connection(ledger_runtime_dsn) as connection:
        page = await LedgerReader().for_run(connection, RUN_ID, trace_id=TRACE_ID)
    assert len(page.events) == 1
    event = page.events[0]
    assert event.actor_type == "POLICY"
    assert event.versions.policy_version == "synthetic-strict@v1"
    assert event.versions.graph_version == "graph@v1"
    assert event.versions.model_version == event.versions.prompt_version == "not-applicable@v1"
    assert ValidationResult.model_validate(event.payload) == result
    if not valid:
        assert result.issues[0].model_dump(mode="json")["difference"] == "75"


class FailAfterAppend:
    async def append(
        self, connection: LedgerConnection, command: AppendEvent, *, trace_id: str
    ) -> LedgerEvent:
        await writer().append(connection, command, trace_id=trace_id)
        raise LedgerStorageError(
            "synthetic post-insert failure", run_id=command.run_id, trace_id=trace_id
        )


async def test_failed_audit_rolls_back_and_never_returns_a_successful_result(
    ledger_runtime_dsn: str,
) -> None:
    sink = TransactionalAuditSink(lambda: runtime_connection(ledger_runtime_dsn), FailAfterAppend())
    request = ValidationRequest(
        run_id=RUN_ID, invoice_id=INVOICE_ID, trace_id=TRACE_ID, extraction=invoice()
    )
    with pytest.raises(LedgerStorageError):
        await ValidateNode(sink).run(request)
    async with runtime_connection(ledger_runtime_dsn) as connection:
        page = await LedgerReader().for_run(connection, RUN_ID, trace_id=TRACE_ID)
    assert page.events == []
