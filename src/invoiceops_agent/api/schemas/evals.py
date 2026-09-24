"""Versioned evaluation summaries suitable for the operations console."""

from datetime import date
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from invoiceops_agent.schemas.common import ExactDecimal


class EvalModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class MetricRow(EvalModel):
    key: str = Field(min_length=1, max_length=128)
    label: str = Field(min_length=1, max_length=128)
    scope: str = Field(min_length=1, max_length=128)
    value: ExactDecimal
    unit: Literal["rate", "usd", "ms", "count"]
    sample_count: int | None = Field(default=None, ge=0)
    tp: int | None = Field(default=None, ge=0)
    fp: int | None = Field(default=None, ge=0)
    fn: int | None = Field(default=None, ge=0)


class AnomalyConfusion(EvalModel):
    anomaly_code: str = Field(min_length=1, max_length=128)
    tp: int = Field(ge=0)
    fp: int = Field(ge=0)
    fn: int = Field(ge=0)
    tn: int = Field(ge=0)


class TauSweepPoint(EvalModel):
    threshold: ExactDecimal = Field(ge=0, le=1)
    exception_recall: ExactDecimal = Field(ge=0, le=1)
    false_escalation_rate: ExactDecimal = Field(ge=0, le=1)
    stp_rate: ExactDecimal = Field(ge=0, le=1)


class EvalReport(EvalModel):
    report_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,99}$")
    report_version: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=200)
    dataset_version: str = Field(min_length=1, max_length=128)
    measured_at: AwareDatetime
    model_versions: list[str] = Field(max_length=20)
    metrics: list[MetricRow] = Field(max_length=500)
    per_anomaly_confusion: list[AnomalyConfusion] = Field(max_length=100)
    tau_sweep: list[TauSweepPoint] = Field(max_length=101)
    caveats: list[str] = Field(max_length=20)


class ExperimentEntry(EvalModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,99}$")
    report_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,99}$")
    date: date
    hypothesis: str = Field(min_length=1, max_length=1000)
    change: str = Field(min_length=1, max_length=1000)
    observation: str = Field(min_length=1, max_length=1000)
    decision: str = Field(min_length=1, max_length=1000)


class EvalDashboard(EvalModel):
    reports: list[EvalReport] = Field(max_length=50)
    experiments: list[ExperimentEntry] = Field(max_length=200)
    read_at: AwareDatetime
