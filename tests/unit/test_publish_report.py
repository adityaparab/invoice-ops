"""Published Evals summaries preserve measured values and unavailable coverage."""

from decimal import Decimal

import pytest
from eval.diagnostics import DiagnosticReport, score_diagnostics
from eval.metrics import PrimaryMetricsReport, score_primary_metrics
from eval.publish_report import PublicationEvidenceError, publish_report
from tests.unit.test_primary_metrics import MANIFEST, MANIFEST_SHA, _record, _report, _sample

pytestmark = pytest.mark.unit


def _sources() -> tuple[PrimaryMetricsReport, DiagnosticReport]:
    sample = _sample()
    pipelines = tuple(
        _report((_record(sample, route="ARCHIVE", run_number=index),), offset_minutes=index)
        for index in range(3)
    )
    return (
        score_primary_metrics(MANIFEST, MANIFEST_SHA, pipelines),
        score_diagnostics(MANIFEST, MANIFEST_SHA, pipelines[0]),
    )


def test_partial_publication_preserves_latency_units_and_missing_measurements() -> None:
    metrics, diagnostics = _sources()
    report = publish_report(
        metrics,
        diagnostics,
        report_id="golden-development-v1",
        title="Synthetic development measurement",
    )
    by_key = {row.key: row for row in report.metrics}
    assert report.report_version == "pipeline-eval@v1"
    assert report.dataset_version == "golden/v1.0.0"
    assert by_key["p95_latency_ms"].value == 5000
    assert by_key["p95_latency_ms"].unit == "ms"
    assert "exception_recall" not in by_key
    assert report.tau_sweep == []
    assert any("Unavailable primary" in caveat for caveat in report.caveats)
    assert '"5000"' in report.model_dump_json()


def test_publication_rejects_mismatched_manifest_and_changed_targets() -> None:
    metrics, diagnostics = _sources()
    with pytest.raises(PublicationEvidenceError, match="differ"):
        publish_report(
            metrics,
            diagnostics.model_copy(update={"manifest_sha256": "f" * 64}),
            report_id="golden-development-v1",
            title="Synthetic development measurement",
        )
    changed = metrics.metrics[0].model_copy(update={"target": Decimal("0.5")})
    with pytest.raises(PublicationEvidenceError, match="versioned"):
        publish_report(
            metrics.model_copy(update={"metrics": (changed, *metrics.metrics[1:])}),
            diagnostics,
            report_id="golden-development-v1",
            title="Synthetic development measurement",
        )
