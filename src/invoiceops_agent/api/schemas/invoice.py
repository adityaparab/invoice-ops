"""HTTP response for an accepted upload; queued does not imply processing has started."""

from invoiceops_agent.tools.ingestion_schemas import IngestionResult


class InvoiceUploadResponse(IngestionResult):
    pass
