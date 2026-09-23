"""Shared ingestion contracts, independent of HTTP and persistence adapters."""

from dataclasses import dataclass, field
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

DocumentType = Literal["application/pdf", "image/png", "image/jpeg"]


@dataclass(frozen=True)
class RawDocument:
    content_type: DocumentType
    content_hash: str
    request_hash: str
    body: bytes = field(repr=False)

    @property
    def object_key(self) -> str:
        return f"sha256/{self.content_hash[:2]}/{self.content_hash}"


class IngestionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    invoice_id: UUID
    run_id: UUID
    status: Literal["QUEUED"] = "QUEUED"
    duplicate: Literal[False] = False
