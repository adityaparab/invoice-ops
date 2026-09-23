"""Sanitized document preparation failures with request correlation."""

from uuid import UUID


class DocumentError(Exception):
    code = "INVALID_DOCUMENT"

    def __init__(self, *, run_id: UUID, trace_id: str) -> None:
        self.run_id = run_id
        self.trace_id = trace_id
        super().__init__(f"{self.code} run_id={run_id} trace_id={trace_id}")


class DocumentUnavailable(DocumentError):
    code = "DOCUMENT_UNAVAILABLE"


class UnsupportedDocumentFeature(DocumentError):
    code = "UNSUPPORTED_DOCUMENT"
