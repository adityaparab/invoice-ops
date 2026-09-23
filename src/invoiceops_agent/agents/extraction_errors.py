"""Operational failures that cannot truthfully produce an audited extraction outcome."""

from invoiceops_agent.schemas.extraction import ExtractionRequest


class ExtractionError(Exception):
    code = "extraction_error"

    def __init__(self, request: ExtractionRequest) -> None:
        self.run_id = request.run_id
        self.trace_id = request.trace_id
        super().__init__(f"{self.code} run_id={self.run_id} trace_id={self.trace_id}")


class ExtractionConfigurationError(ExtractionError):
    code = "extraction_configuration"


class ExtractionAuditFailed(ExtractionError):
    code = "extraction_audit_failed"
