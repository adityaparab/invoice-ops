"""Versioned near-duplicate decision and model-vector contracts."""

import math
from decimal import Decimal
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from invoiceops_agent.schemas.common import ExactDecimal
from invoiceops_agent.schemas.extraction import InvoiceExtraction

EMBEDDING_DIMENSIONS = 384
SUMMARY_VERSION = "near-duplicate-summary@v1"


class SimilarityModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class SimilarityConfig(SimilarityModel):
    version: str = Field(default="near-duplicate@v1", pattern=r"^[A-Za-z0-9@_.:-]{1,128}$")
    minimum_cosine_similarity: ExactDecimal = Field(default=Decimal("0.95"), ge=0, le=1)


class SimilarityRequest(SimilarityModel):
    run_id: UUID
    invoice_id: UUID
    trace_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    extraction: InvoiceExtraction = Field(repr=False)


class EmbeddingVector(SimilarityModel):
    values: tuple[float, ...] = Field(
        min_length=EMBEDDING_DIMENSIONS, max_length=EMBEDDING_DIMENSIONS, repr=False
    )

    @model_validator(mode="after")
    def finite_nonzero(self) -> Self:
        if any(not math.isfinite(value) for value in self.values):
            raise ValueError("Embedding coordinates must be finite")
        if not any(value != 0 for value in self.values):
            raise ValueError("Cosine embedding must not be the zero vector")
        return self


class SimilarityCandidate(SimilarityModel):
    invoice_id: UUID
    cosine_similarity: ExactDecimal = Field(ge=-1, le=1)


class SimilarityResult(SimilarityModel):
    status: Literal["NEAR_DUPLICATE", "NO_MATCH"]
    candidate: SimilarityCandidate | None
    extraction_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    embedding_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    summary_version: str = SUMMARY_VERSION
    model_version: str = Field(min_length=1, max_length=160)
    gateway_model: str = Field(min_length=1, max_length=160)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    gateway_latency_ms: float | None = Field(default=None, ge=0)
    gateway_cost_usd: Decimal | None = Field(default=None, ge=0)
    config: SimilarityConfig

    @model_validator(mode="after")
    def consistent_status(self) -> Self:
        expected = "NEAR_DUPLICATE" if self.candidate is not None else "NO_MATCH"
        if self.status != expected:
            raise ValueError("Similarity status must match candidate presence")
        if (
            self.candidate is not None
            and self.candidate.cosine_similarity < self.config.minimum_cosine_similarity
        ):
            raise ValueError("Candidate does not meet the versioned similarity threshold")
        return self
