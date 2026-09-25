"""Declared model classes survive the harness and compare without invented results."""

from decimal import Decimal

import pytest
from eval.diagnostics import score_diagnostics
from eval.metrics import score_primary_metrics
from eval.model_classes import ModelClassComparisonError, compare_model_classes
from eval.runners.schema import PipelineReport
from pydantic import ValidationError
from tests.unit.test_primary_metrics import MANIFEST, MANIFEST_SHA, _record, _report, _sample

pytestmark = pytest.mark.unit


def test_live_pipeline_requires_a_declared_model_class() -> None:
    sample = _sample()
    live = _report((_record(sample, route="AUTO_APPROVE"),), offset_minutes=0)
    missing = live.model_dump(mode="json")
    missing["model_class"] = None
    with pytest.raises(ValidationError, match="declared model class"):
        PipelineReport.model_validate(missing)
    legacy = {**missing, "version": "pipeline-run@v1"}
    assert PipelineReport.model_validate(legacy).model_class is None


def test_class_reports_keep_tags_and_partial_comparisons_explicit() -> None:
    sample = _sample()
    local_pipeline = _report((_record(sample, route="AUTO_APPROVE"),), offset_minutes=0)
    prod_pipeline = PipelineReport.model_validate(
        {**local_pipeline.model_dump(mode="json"), "model_class": "openai-prod"}
    )
    local = score_primary_metrics(MANIFEST, MANIFEST_SHA, (local_pipeline,))
    production = score_primary_metrics(MANIFEST, MANIFEST_SHA, (prod_pipeline,))
    diagnostic = score_diagnostics(MANIFEST, MANIFEST_SHA, prod_pipeline)
    assert local.model_class == "local-dev" and production.model_class == "openai-prod"
    assert diagnostic.model_class == "openai-prod"
    comparison = compare_model_classes(
        local,
        production,
        local_sha256="a" * 64,
        prod_sha256="b" * 64,
    )
    assert not comparison.complete_comparison
    assert len(comparison.rows) == 8
    assert next(row for row in comparison.rows if row.key == "stp_rate").prod_minus_local == 0
    latency = next(row for row in comparison.rows if row.key == "p95_latency_seconds")
    assert latency.prod_minus_local is None


def test_comparison_rejects_wrong_class_and_changed_targets() -> None:
    sample = _sample()
    pipeline = _report((_record(sample, route="AUTO_APPROVE"),), offset_minutes=0)
    local = score_primary_metrics(MANIFEST, MANIFEST_SHA, (pipeline,))
    with pytest.raises(ModelClassComparisonError, match="tagged"):
        compare_model_classes(local, local, local_sha256="a" * 64, prod_sha256="b" * 64)
    production = local.model_copy(update={"model_class": "openai-prod"})
    first = production.metrics[0].model_copy(update={"target": Decimal("0.90")})
    modified = production.model_copy(update={"metrics": (first, *production.metrics[1:])})
    with pytest.raises(ModelClassComparisonError, match="target"):
        compare_model_classes(local, modified, local_sha256="a" * 64, prod_sha256="b" * 64)


def test_adk_gemini_zero_reported_cost_is_qualified() -> None:
    sample = _sample()
    record = _record(sample, route="AUTO_APPROVE", embedding_cost="0")
    evidence = dict(record.detail.evidence)
    extraction = dict(evidence["extraction.completed"])
    result = extraction["result"]
    assert isinstance(result, dict)
    extraction["result"] = {**result, "calls": [{"cost_usd": "0"}]}
    evidence["extraction.completed"] = extraction
    zero_cost = record.model_copy(
        update={"detail": record.detail.model_copy(update={"evidence": evidence})}
    )
    pipeline = _report((zero_cost,), offset_minutes=0).model_copy(
        update={"model_class": "adk-gemini"}
    )
    scored = score_primary_metrics(MANIFEST, MANIFEST_SHA, (pipeline,))
    assert scored.model_class == "adk-gemini"
    assert any("actual provider spend is not established" in note for note in scored.caveats)
