"""Shared ingestion contracts, independent of HTTP and persistence adapters."""

from dataclasses import dataclass, field
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, model_validator

DocumentType = Literal["application/pdf", "image/png", "image/jpeg"]
IngestionSource = Literal["UPLOAD", "EMAIL"]


@dataclass(frozen=True)
class RawDocument:
    content_type: DocumentType
    content_hash: str
    request_hash: str
    body: bytes = field(repr=False)
    source: IngestionSource = "UPLOAD"

    @property
    def object_key(self) -> str:
        return f"sha256/{self.content_hash[:2]}/{self.content_hash}"


class IngestionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    invoice_id: UUID
    run_id: UUID
    status: Literal["QUEUED"] = "QUEUED"
    duplicate: bool = False


class IngestionOutcome(BaseModel):
    """Persist both HTTP status and body so replay preserves the original outcome."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    response_status: Literal[200, 201]
    body: IngestionResult

    @model_validator(mode="after")
    def validate_response_status(self) -> Self:
        if (self.response_status == 200) != self.body.duplicate:
            raise ValueError("Duplicate responses must use 200; accepted responses must use 201")
        return self


class OriginalIngestion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    response_body: IngestionResult
    graph_version: str
    raw_ref: str
