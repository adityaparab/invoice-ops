"""Pure normalization and bounded cosine-similarity decisions."""

from decimal import Decimal
from math import isfinite
from uuid import UUID

from invoiceops_agent.schemas.extraction import InvoiceExtraction
from invoiceops_agent.schemas.similarity import SimilarityCandidate, SimilarityConfig


def invoice_summary(extraction: InvoiceExtraction) -> str:
    """Build a stable text summary without bank or tax identifiers."""
    fields = (
        ("vendor", extraction.vendor_name.value),
        ("invoice", extraction.invoice_number.value),
        ("po", extraction.po_number.value),
        ("currency", extraction.currency.value),
        ("date", extraction.invoice_date.value),
        ("net", extraction.subtotal.value),
        ("tax", extraction.tax_amount.value),
        ("total", extraction.total_amount.value),
    )
    lines = [
        f"line {number}: {line.description.value or ''} | quantity={line.quantity.value} "
        f"| price={line.unit_price.value} | total={line.line_total.value}"
        for number, line in enumerate(extraction.line_items, start=1)
    ]
    return "\n".join(
        [f"{name}={value if value is not None else ''}" for name, value in fields] + lines
    )


def choose_candidate(
    invoice_id: UUID | None,
    cosine_distance: float | None,
    config: SimilarityConfig,
) -> SimilarityCandidate | None:
    """Convert pgvector's cosine distance to the versioned review threshold."""
    if invoice_id is None or cosine_distance is None:
        return None
    if not isfinite(cosine_distance):
        raise ValueError("Cosine distance must be finite")
    similarity = Decimal(1) - Decimal(str(cosine_distance))
    if not Decimal(-1) <= similarity <= Decimal(1):
        raise ValueError("Cosine similarity is outside its mathematical range")
    if similarity < config.minimum_cosine_similarity:
        return None
    return SimilarityCandidate(invoice_id=invoice_id, cosine_similarity=similarity)
