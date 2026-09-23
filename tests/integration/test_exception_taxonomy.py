"""Exception classification commits a versioned decision to the append-only ledger."""

import pytest
from tests.integration.support import INVOICE_ID, RUN_ID
from tests.integration.test_ledger import ledger_runtime_dsn as ledger_runtime_dsn
from tests.integration.test_ledger import runtime_connection, writer
from tests.unit.matching_support import matching_request, snapshot_for

from invoiceops_agent.graph.nodes.exception_taxonomy import ExceptionTaxonomyNode
from invoiceops_agent.ledger.audit import TransactionalAuditSink
from invoiceops_agent.ledger.reader import LedgerReader
from invoiceops_agent.schemas.exceptions import TaxonomyRequest, TaxonomyResult
from invoiceops_agent.tools.matching import match_invoice
from invoiceops_agent.tools.validation import validate_invoice

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_classification_node_commits_evidence_before_return(
    ledger_runtime_dsn: str,
) -> None:
    source = matching_request(snapshot_for("CLOSED"))
    request = TaxonomyRequest(
        run_id=RUN_ID,
        invoice_id=INVOICE_ID,
        trace_id="c" * 32,
        extraction=source.extraction,
        validation=validate_invoice(source.extraction),
        match=match_invoice(source),
        vendor_bank_iban=source.snapshot.vendor.bank_account_iban
        if source.snapshot is not None
        else None,
    )
    sink = TransactionalAuditSink(lambda: runtime_connection(ledger_runtime_dsn), writer())
    result = await ExceptionTaxonomyNode(sink).run(request)
    async with runtime_connection(ledger_runtime_dsn) as connection:
        page = await LedgerReader().for_run(connection, RUN_ID, trace_id="c" * 32)
    assert len(page.events) == 1
    event = page.events[0]
    assert event.event_type == "classification.completed"
    assert event.actor_type == "POLICY"
    assert event.versions.policy_version == "exception-taxonomy@v1"
    assert TaxonomyResult.model_validate(event.payload) == result
    assert result.codes == ("STALE_PO",)
