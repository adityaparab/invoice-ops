"""Bounded, incremental hashing and deterministic declared-type/signature validation."""

import hashlib
from typing import Protocol, cast

from invoiceops_agent.tools.ingestion_errors import (
    DocumentTooLarge,
    InvalidDocument,
    UnsupportedDocument,
)
from invoiceops_agent.tools.ingestion_schemas import DocumentType, RawDocument

CHUNK_SIZE = 64 * 1024
SIGNATURES = {
    "application/pdf": b"%PDF-",
    "image/png": b"\x89PNG\r\n\x1a\n",
    "image/jpeg": b"\xff\xd8\xff",
}


class AsyncDocument(Protocol):
    async def read(self, size: int = -1) -> bytes: ...


async def read_document(
    stream: AsyncDocument, content_type: str | None, *, max_bytes: int
) -> RawDocument:
    """Read at most one bounded document; filenames are not request semantics."""
    if content_type not in SIGNATURES:
        raise UnsupportedDocument("Only PDF, PNG, and JPEG documents are accepted.")
    digest = hashlib.sha256()
    data = bytearray()
    while chunk := await stream.read(CHUNK_SIZE):
        if len(data) + len(chunk) > max_bytes:
            raise DocumentTooLarge("Document exceeds the configured size limit.")
        digest.update(chunk)
        data.extend(chunk)
    if not data:
        raise InvalidDocument("The document is empty.")
    if not data.startswith(SIGNATURES[content_type]):
        raise UnsupportedDocument("Document signature does not match its declared media type.")
    content_hash = digest.hexdigest()
    request_hash = hashlib.sha256(
        f"UPLOAD\n{content_type}\n{content_hash}".encode("ascii")
    ).hexdigest()
    return RawDocument(cast(DocumentType, content_type), content_hash, request_hash, bytes(data))
