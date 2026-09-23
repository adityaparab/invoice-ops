"""Immutable raw-object references shared by ingestion and extraction."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

DocumentType = Literal["application/pdf", "image/png", "image/jpeg"]


class DocumentReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    raw_ref: str = Field(min_length=1, max_length=256, repr=False)
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    content_type: DocumentType
