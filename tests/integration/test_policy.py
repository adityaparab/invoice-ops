"""Versioned policy decisions commit to the append-only ledger."""

import pytest
from tests.integration.support import INVOICE_ID, RUN_ID
from tests.integration.test_ledger import ledger_runtime_dsn as ledger_runtime_dsn
from tests.integration.test_ledger import runtime_connection, writer
from tests.unit.policy_support import policy_request

from invoiceops_agent.graph.nodes.policy import PolicyNode
from invoiceops_agent.ledger.audit import TransactionalAuditSink
from invoiceops_agent.ledger.reader import LedgerReader
from invoiceops_agent.schemas.policy import PolicyResult

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_policy_node_commits_versioned_decision(ledger_runtime_dsn: str) -> None:
    sink = TransactionalAuditSink(lambda: runtime_connection(ledger_runtime_dsn), writer())
    request = policy_request(exact_duplicate=True).model_copy(
        update={"run_id": RUN_ID, "invoice_id": INVOICE_ID, "trace_id": "b" * 32}
    )
    result = await PolicyNode(sink).run(request)
    async with runtime_connection(ledger_runtime_dsn) as connection:
        page = await LedgerReader().for_run(connection, RUN_ID, trace_id="b" * 32)
    assert result.status == "BLOCK"
    assert len(page.events) == 1
    event = page.events[0]
    assert event.event_type == "policy.completed"
    assert event.actor_type == "POLICY"
    assert event.versions.policy_version == "invoice-policy@v1"
    assert PolicyResult.model_validate(event.payload) == result
