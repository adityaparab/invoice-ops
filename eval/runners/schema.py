"""Auditable output contracts for a complete pipeline evaluation run."""

from datetime import UTC, datetime
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from eval.golden.schema import AnomalyCode, Split
from invoiceops_agent.api.schemas.invoice_read import InvoiceDetail
from invoiceops_agent.api.schemas.provenance import InvoiceProvenancePage
from invoiceops_agent.tools.ingestion_schemas import IngestionResult

ModelClass = Literal["local-dev", "openai-prod", "adk-gemini"]


class RunRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sample_id: str
    split: Split
    expected_codes: tuple[AnomalyCode, ...]
    document_sha256: str
    upload: IngestionResult
    upload_duration_ms: float = Field(ge=0)
    worker_duration_ms: float | None = Field(default=None, ge=0)
    replayed_before_worker: bool = False
    route: str
    worker_error_type: str | None = None
    detail: InvoiceDetail
    provenance: InvoiceProvenancePage


class PipelineReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal["pipeline-run@v1", "pipeline-run@v2"] = "pipeline-run@v2"
    dataset_version: Literal["golden/v1.0.0", "golden/v1.0.1"] = "golden/v1.0.1"
    mode: Literal["live", "recorded"]
    model_class: ModelClass | None = None
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    started_at: AwareDatetime
    completed_at: AwareDatetime
    samples: tuple[RunRecord, ...]

    @model_validator(mode="after")
    def coherent_class(self) -> "PipelineReport":
        if self.mode == "recorded" and self.model_class is not None:
            raise ValueError("Recorded pipeline smoke cannot claim a model class")
        if self.version == "pipeline-run@v2" and self.mode == "live" and self.model_class is None:
            raise ValueError("Version 2 live runs require a declared model class")
        return self

    @property
    def elapsed_seconds(self) -> float:
        return (self.completed_at - self.started_at).total_seconds()


def utc_now() -> datetime:
    return datetime.now(UTC)
