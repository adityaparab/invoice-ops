"""Measured report projection and future pipeline chart inputs without model calls."""

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from invoiceops_agent.api.eval_reader import EvalReportsUnavailable, FileEvalReader
from invoiceops_agent.api.settings import ApiSettings

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]
ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 9, 24, tzinfo=UTC)


async def test_committed_reports_distinguish_measured_development_evidence() -> None:
    reader = FileEvalReader(ApiSettings(eval_reports_dir=ROOT / "eval/reports"), clock=lambda: NOW)
    dashboard = await reader.dashboard()
    assert dashboard.read_at == NOW
    reports = {report.report_id: report for report in dashboard.reports}
    assert len(reports) >= 3
    development = reports["golden-openai-prod-development-v01"]
    assert development.report_id == "golden-openai-prod-development-v01"
    assert development.report_version == "pipeline-eval@v1"
    assert any(row.key == "exception_recall" and row.value > 0 for row in development.metrics)
    assert all(row.key != "cost_per_invoice_usd" for row in development.metrics)
    assert len(development.per_anomaly_confusion) == 10
    assert any("partial" in caveat.lower() for caveat in development.caveats)
    improved = reports["golden-openai-prod-development-v06"]
    assert next(row for row in improved.metrics if row.key == "exception_recall").value == 1
    assert next(row for row in improved.metrics if row.key == "cost_per_invoice_usd").value > 0
    assert any("partial" in caveat.lower() for caveat in improved.caveats)
    report = reports["extraction-baseline-v1"]
    assert report.report_version == "extraction-baseline@v1"
    assert report.metrics[0].key == "micro-f1"
    assert report.metrics[0].value == Decimal("0.7226")
    assert report.metrics[0].sample_count == 512
    assert report.per_anomaly_confusion == []
    assert report.tau_sweep == []
    assert len(dashboard.experiments) >= 3
    assert {row.report_id for row in dashboard.experiments} <= set(reports)
    assert {row.report_id for row in dashboard.experiments} >= {
        report.report_id,
        development.report_id,
        improved.report_id,
    }
    assert '"value":"0.7226"' in dashboard.model_dump_json()


async def test_pipeline_report_populates_confusion_and_threshold_sweep(tmp_path: Path) -> None:
    report = {
        "report_id": "pipeline-eval-v1",
        "report_version": "pipeline-eval@v1",
        "title": "Synthetic pipeline evaluation",
        "dataset_version": "golden/v1.0.0",
        "measured_at": "2026-09-24T00:00:00Z",
        "model_versions": ["synthetic-model"],
        "metrics": [
            {
                "key": "exception-recall",
                "label": "Exception recall",
                "scope": "overall",
                "value": "0.98",
                "unit": "rate",
                "sample_count": 10,
            }
        ],
        "per_anomaly_confusion": [
            {
                "anomaly_code": "AMOUNT_MISMATCH",
                "tp": 9,
                "fp": 1,
                "fn": 1,
                "tn": 89,
            }
        ],
        "tau_sweep": [
            {
                "threshold": "0.95",
                "exception_recall": "0.98",
                "false_escalation_rate": "0.02",
                "stp_rate": "0.72",
            }
        ],
        "caveats": [],
    }
    (tmp_path / "pipeline-eval-v1.json").write_text(json.dumps(report), encoding="utf-8")
    dashboard = await FileEvalReader(ApiSettings(eval_reports_dir=tmp_path)).dashboard()
    assert dashboard.reports[0].per_anomaly_confusion[0].tp == 9
    assert dashboard.reports[0].tau_sweep[0].threshold == Decimal("0.95")


async def test_malformed_report_is_sanitized(tmp_path: Path) -> None:
    (tmp_path / "pipeline-eval-bad.json").write_text(
        '{"report_version":"unknown"}', encoding="utf-8"
    )
    with pytest.raises(EvalReportsUnavailable, match="could not be read"):
        await FileEvalReader(ApiSettings(eval_reports_dir=tmp_path)).dashboard()
