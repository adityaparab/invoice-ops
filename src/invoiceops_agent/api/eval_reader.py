"""Bounded, validated reads of committed synthetic evaluation reports."""

import asyncio
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Literal, Protocol

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

from invoiceops_agent.api.schemas.evals import EvalDashboard, EvalReport, ExperimentEntry, MetricRow
from invoiceops_agent.api.settings import ApiSettings
from invoiceops_agent.schemas.common import ExactDecimal

logger = logging.getLogger(__name__)
_MAX_FILE_BYTES = 1_000_000


class EvalReadError(Exception):
    """Sanitized report-read failure."""


class EvalReportsUnavailable(EvalReadError):
    """Committed evaluation reports could not be read."""


class EvalReader(Protocol):
    async def dashboard(self) -> EvalDashboard: ...


class _SourceModel(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, allow_inf_nan=False)


class _Score(_SourceModel):
    eligible: int = Field(ge=0)
    tp: int = Field(ge=0)
    fp: int = Field(ge=0)
    fn: int = Field(ge=0)
    precision: ExactDecimal = Field(ge=0, le=1)
    recall: ExactDecimal = Field(ge=0, le=1)
    f1: ExactDecimal = Field(ge=0, le=1)


class _Tier(_SourceModel):
    sample_count: int = Field(ge=0)
    overall: _Score
    fields: dict[str, _Score]


class _Dataset(_SourceModel):
    pipeline_version: str
    sample_count: int = Field(ge=0)


class _Baseline(_SourceModel):
    report_version: Literal["extraction-baseline@v1"]
    measured_at: AwareDatetime
    model_versions: list[str]
    dataset: _Dataset
    tier_metrics: dict[Literal["A", "B", "C"], _Tier | None]


class _ExperimentLog(_SourceModel):
    version: Literal["experiment-log@v1"]
    entries: list[ExperimentEntry]


def _read_json(path: Path, root: Path) -> object:
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(root) or resolved.stat().st_size > _MAX_FILE_BYTES:
        raise ValueError("Report path or size is invalid")
    return json.loads(resolved.read_text(encoding="utf-8"))


def _baseline_view(raw: object) -> EvalReport:
    report = _Baseline.model_validate(raw)
    metrics: list[MetricRow] = []
    tier_names: tuple[Literal["A", "B", "C"], ...] = ("A", "B", "C")
    for tier_name in tier_names:
        tier = report.tier_metrics.get(tier_name)
        if tier is None:
            continue
        metrics.append(_score_metric("micro-f1", "Micro field F1", tier_name, tier.overall))
        for name, score in sorted(tier.fields.items()):
            metrics.append(
                _score_metric(
                    f"{name}-f1", name.replace("_", " ").title() + " F1", tier_name, score
                )
            )
    return EvalReport(
        report_id="extraction-baseline-v1",
        report_version=report.report_version,
        title="Extraction development baseline",
        dataset_version=report.dataset.pipeline_version,
        measured_at=report.measured_at,
        model_versions=report.model_versions,
        metrics=metrics,
        per_anomaly_confusion=[],
        tau_sweep=[],
        caveats=[
            "Development subset only; this is not a held-out or release-gate evaluation.",
            "Only tier A was measured. Tiers B and C are unavailable, not zero.",
            "Per-anomaly confusion and threshold sweep await the golden-set harness.",
        ],
    )


def _score_metric(key: str, label: str, tier: str, score: _Score) -> MetricRow:
    return MetricRow(
        key=key,
        label=label,
        scope=f"Tier {tier}",
        value=score.f1,
        unit="rate",
        sample_count=score.eligible,
        tp=score.tp,
        fp=score.fp,
        fn=score.fn,
    )


class FileEvalReader:
    def __init__(
        self, settings: ApiSettings, *, clock: Callable[[], datetime] | None = None
    ) -> None:
        self._root = settings.eval_reports_dir
        self._clock = clock if clock is not None else lambda: datetime.now(UTC)

    async def dashboard(self) -> EvalDashboard:
        started = perf_counter()
        try:
            reports, experiments = await asyncio.to_thread(self._load)
            result = EvalDashboard(
                reports=reports,
                experiments=experiments,
                read_at=self._clock(),
            )
        except (OSError, ValueError, json.JSONDecodeError, ValidationError) as error:
            logger.error("eval_reports_read_failed error_type=%s", type(error).__name__)
            raise EvalReportsUnavailable("Evaluation reports could not be read") from error
        logger.info(
            "eval_reports_read reports=%d experiments=%d duration_ms=%.3f",
            len(result.reports),
            len(result.experiments),
            (perf_counter() - started) * 1000,
        )
        return result

    def _load(self) -> tuple[list[EvalReport], list[ExperimentEntry]]:
        root = self._root.resolve(strict=True)
        if not root.is_dir():
            raise ValueError("Report directory is invalid")
        reports: list[EvalReport] = []
        baseline_path = root / "extraction-baseline-v1.json"
        if baseline_path.exists():
            reports.append(_baseline_view(_read_json(baseline_path, root)))
        pipeline_paths = sorted(root.glob("pipeline-eval-*.json"))
        if len(pipeline_paths) > 49:
            raise ValueError("Too many pipeline reports")
        for path in pipeline_paths:
            report = EvalReport.model_validate(_read_json(path, root))
            if report.report_version != "pipeline-eval@v1":
                raise ValueError("Pipeline report version is unsupported")
            reports.append(report)
        log_path = root / "experiment-log-v1.json"
        experiments = (
            _ExperimentLog.model_validate(_read_json(log_path, root)).entries
            if log_path.exists()
            else []
        )
        report_ids = {report.report_id for report in reports}
        if len(report_ids) != len(reports) or any(
            entry.report_id not in report_ids for entry in experiments
        ):
            raise ValueError("Experiment references or report IDs are invalid")
        reports.sort(key=lambda report: (report.measured_at, report.report_id), reverse=True)
        return reports, experiments
