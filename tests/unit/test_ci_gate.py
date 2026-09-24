"""The release gate needs complete evidence and exact regression boundaries."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from eval.ci_gate import GateEvidenceError, compare_reports, render_comment
from eval.metrics import PRIMARY_TARGETS, PrimaryMetric, PrimaryMetricsReport

pytestmark = pytest.mark.unit


def _report(**values: Decimal) -> PrimaryMetricsReport:
    metrics = tuple(
        PrimaryMetric(
            key=key,
            target=target.value,
            direction=target.direction,
            value=values.get(key, target.value),
            sample_count=500,
            evidence_count=500,
        )
        for key, target in PRIMARY_TARGETS.items()
    )
    return PrimaryMetricsReport(
        manifest_sha256="a" * 64,
        mode="live",
        model_class="openai-prod",
        report_count=3,
        sample_count=500,
        complete_suite=True,
        scored_at=datetime(2026, 9, 24, tzinfo=UTC),
        metrics=metrics,
    )


def _compare(baseline: PrimaryMetricsReport, candidate: PrimaryMetricsReport) -> tuple[bool, str]:
    result = compare_reports(
        baseline,
        candidate,
        baseline_sha256="b" * 64,
        candidate_sha256="c" * 64,
    )
    return result.passed, render_comment(result)


def test_equal_floor_values_pass_and_render_deltas() -> None:
    passed, comment = _compare(_report(), _report())
    assert passed
    assert "Golden evaluation gate: PASS" in comment
    assert "`money_field_f1`" in comment
    assert "0.5 percentage points" in comment


def test_exact_regression_boundary_passes_if_floor_still_holds() -> None:
    baseline = _report(exception_recall=Decimal("0.99"))
    candidate = _report(exception_recall=Decimal("0.985"))
    assert _compare(baseline, candidate)[0]


def test_rate_regression_and_floor_breaches_fail_independently() -> None:
    baseline = _report(exception_recall=Decimal("0.99"), stp_rate=Decimal("0.71"))
    regression = _report(exception_recall=Decimal("0.9849"), stp_rate=Decimal("0.71"))
    floor = _report(exception_recall=Decimal("0.99"), stp_rate=Decimal("0.6999"))
    assert "regression" in _compare(baseline, regression)[1]
    assert "floor" in _compare(baseline, floor)[1]
    assert not _compare(baseline, regression)[0]
    assert not _compare(baseline, floor)[0]


def test_cost_and_latency_use_half_percent_of_target_in_native_units() -> None:
    baseline = _report(cost_per_invoice_usd=Decimal("0.03"), p95_latency_seconds=Decimal(40))
    boundary = _report(
        cost_per_invoice_usd=Decimal("0.0302"), p95_latency_seconds=Decimal("40.225")
    )
    exceeded = _report(
        cost_per_invoice_usd=Decimal("0.030201"), p95_latency_seconds=Decimal("40.226")
    )
    assert _compare(baseline, boundary)[0]
    assert not _compare(baseline, exceeded)[0]


@pytest.mark.parametrize(
    "change",
    [
        {"mode": "recorded"},
        {"model_class": None},
        {"report_count": 1},
        {"sample_count": 499},
        {"complete_suite": False},
    ],
)
def test_incomplete_reports_are_rejected(change: dict[str, object]) -> None:
    with pytest.raises(GateEvidenceError, match="complete"):
        _compare(_report(), _report().model_copy(update=change))


def test_missing_cost_and_mismatched_targets_are_rejected() -> None:
    candidate = _report()
    cost = candidate.metrics[6].model_copy(update={"evidence_count": 499})
    with pytest.raises(GateEvidenceError, match="Cost evidence"):
        _compare(
            _report(),
            candidate.model_copy(
                update={"metrics": (*candidate.metrics[:6], cost, candidate.metrics[7])}
            ),
        )
    changed = candidate.metrics[0].model_copy(update={"target": Decimal("0.9")})
    with pytest.raises(GateEvidenceError, match="target"):
        _compare(
            _report(), candidate.model_copy(update={"metrics": (changed, *candidate.metrics[1:])})
        )


def test_different_golden_manifests_or_classes_are_rejected() -> None:
    with pytest.raises(GateEvidenceError, match="differ"):
        _compare(_report(), _report().model_copy(update={"manifest_sha256": "f" * 64}))
    with pytest.raises(GateEvidenceError, match="differ"):
        _compare(_report(), _report().model_copy(update={"model_class": "local-dev"}))
    local = _report().model_copy(update={"model_class": "local-dev"})
    with pytest.raises(GateEvidenceError, match="differ"):
        _compare(local, local)
