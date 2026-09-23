"""Coherent synthetic evidence for deterministic policy tests."""

from datetime import timedelta

from tests.unit.matching_support import matching_request, snapshot_for

from invoiceops_agent.schemas.exceptions import TaxonomyRequest
from invoiceops_agent.schemas.extraction import InvoiceExtraction
from invoiceops_agent.schemas.matching import ERPSnapshot
from invoiceops_agent.schemas.policy import PolicyRequest
from invoiceops_agent.tools.exception_taxonomy import classify_exceptions
from invoiceops_agent.tools.matching import match_invoice
from invoiceops_agent.tools.validation import validate_invoice


def current_snapshot() -> ERPSnapshot:
    source = snapshot_for("CLOSED")
    return source.model_copy(
        update={"purchase_order": source.purchase_order.model_copy(update={"status": "OPEN"})}
    )


def policy_request(
    *, exact_duplicate: bool = False, extraction: InvoiceExtraction | None = None
) -> PolicyRequest:
    snapshot = current_snapshot()
    source = matching_request(snapshot)
    if extraction is not None:
        source = source.model_copy(update={"extraction": extraction})
    validation = validate_invoice(source.extraction)
    match = match_invoice(source)
    taxonomy = classify_exceptions(
        TaxonomyRequest(
            run_id=source.run_id,
            invoice_id=source.invoice_id,
            trace_id=source.trace_id,
            extraction=source.extraction,
            validation=validation,
            match=match,
            vendor_bank_iban=snapshot.vendor.bank_account_iban,
            exact_duplicate=exact_duplicate,
        )
    )
    return PolicyRequest(
        run_id=source.run_id,
        invoice_id=source.invoice_id,
        trace_id=source.trace_id,
        as_of=snapshot.purchase_order.issued_on + timedelta(days=30),
        extraction=source.extraction,
        validation=validation,
        match=match,
        taxonomy=taxonomy,
        snapshot=snapshot,
    )
