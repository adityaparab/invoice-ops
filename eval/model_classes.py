"""Side-by-side primary reports for declared local and production model classes."""

import argparse
import hashlib
import logging
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

from eval.metrics import PRIMARY_TARGETS, MetricKey, PrimaryMetric, PrimaryMetricsReport
from invoiceops_agent.artifacts import write_new_artifact

logger = logging.getLogger(__name__)


class ModelClassComparisonError(ValueError):
    """Class reports are mislabeled, incomplete, or refer to different gold evidence."""


class ComparisonRow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    key: MetricKey
    direction: Literal["min", "max"]
    target: Decimal
    local_dev: Decimal | None
    openai_prod: Decimal | None
    prod_minus_local: Decimal | None
    local_evidence_count: int = Field(ge=0)
    prod_evidence_count: int = Field(ge=0)


class ModelClassComparison(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    version: Literal["model-class-comparison@v1"] = "model-class-comparison@v1"
    dataset_version: Literal["golden/v1.0.0"] = "golden/v1.0.0"
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scored_at: AwareDatetime
    complete_comparison: bool
    local_report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    prod_report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    local: PrimaryMetricsReport
    production: PrimaryMetricsReport
    rows: tuple[ComparisonRow, ...]
    caveats: tuple[str, ...] = ()


def _metric_map(report: PrimaryMetricsReport) -> dict[MetricKey, PrimaryMetric]:
    by_key = {row.key: row for row in report.metrics}
    if len(by_key) != len(report.metrics) or set(by_key) != set(PRIMARY_TARGETS):
        raise ModelClassComparisonError("Class report has missing or duplicate primary metrics")
    for key, row in by_key.items():
        target = PRIMARY_TARGETS[key]
        if row.target != target.value or row.direction != target.direction:
            raise ModelClassComparisonError("Class report changes a versioned primary target")
    return by_key


def _comparison_row(
    key: MetricKey, local: PrimaryMetric, production: PrimaryMetric
) -> ComparisonRow:
    local_value = local.value
    prod_value = production.value
    return ComparisonRow(
        key=key,
        direction=PRIMARY_TARGETS[key].direction,
        target=PRIMARY_TARGETS[key].value,
        local_dev=local_value,
        openai_prod=prod_value,
        prod_minus_local=(
            prod_value - local_value if prod_value is not None and local_value is not None else None
        ),
        local_evidence_count=local.evidence_count,
        prod_evidence_count=production.evidence_count,
    )


def compare_model_classes(
    local: PrimaryMetricsReport,
    production: PrimaryMetricsReport,
    *,
    local_sha256: str,
    prod_sha256: str,
) -> ModelClassComparison:
    if (
        local.model_class != "local-dev"
        or production.model_class != "openai-prod"
        or local.mode != "live"
        or production.mode != "live"
    ):
        raise ModelClassComparisonError("Comparison requires live, explicitly tagged class reports")
    if (
        local.manifest_sha256 != production.manifest_sha256
        or local.dataset_version != production.dataset_version
        or local.sample_count != production.sample_count
    ):
        raise ModelClassComparisonError("Class reports differ in dataset or selected sample count")
    local_rows = _metric_map(local)
    prod_rows = _metric_map(production)
    rows = tuple(_comparison_row(key, local_rows[key], prod_rows[key]) for key in PRIMARY_TARGETS)
    complete = (
        local.complete_suite
        and production.complete_suite
        and local.report_count == production.report_count == 3
        and all(row.local_dev is not None and row.openai_prod is not None for row in rows)
    )
    caveats = (
        ()
        if complete
        else ("At least one class lacks a complete three-run, 500-invoice live measurement.",)
    )
    return ModelClassComparison(
        manifest_sha256=local.manifest_sha256,
        scored_at=max(local.scored_at, production.scored_at),
        complete_comparison=complete,
        local_report_sha256=local_sha256,
        prod_report_sha256=prod_sha256,
        local=local,
        production=production,
        rows=rows,
        caveats=caveats,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local", type=Path, required=True)
    parser.add_argument("--production", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        local_raw = args.local.read_bytes()
        prod_raw = args.production.read_bytes()
        report = compare_model_classes(
            PrimaryMetricsReport.model_validate_json(local_raw),
            PrimaryMetricsReport.model_validate_json(prod_raw),
            local_sha256=hashlib.sha256(local_raw).hexdigest(),
            prod_sha256=hashlib.sha256(prod_raw).hexdigest(),
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        write_new_artifact(args.output, (report.model_dump_json(indent=2) + "\n").encode())
    except (ModelClassComparisonError, ValidationError, OSError, ValueError) as error:
        logger.error("model_class_comparison_failed error_type=%s", type(error).__name__)
        return 1
    logger.info(
        "model_class_comparison_written output=%s complete=%s",
        args.output,
        report.complete_comparison,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
