"""Typed ingestion failures; transport maps these to sanitized problem details."""


class IngestionError(Exception):
    """Base for expected ingestion failures."""


class InvalidDocument(IngestionError):
    pass


class UnsupportedDocument(IngestionError):
    pass


class DocumentTooLarge(IngestionError):
    pass


class IdempotencyConflict(IngestionError):
    pass


class IngestionUnavailable(IngestionError):
    pass
