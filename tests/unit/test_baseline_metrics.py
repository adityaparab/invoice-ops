"""Exact-value baseline scoring is deterministic and never guesses missing labels."""

import pytest
from eval.baseline.metrics import Manifest, Sample, score_sample, summarize
from pydantic import ValidationError
from tests.unit.extraction_support import invoice_extraction

pytestmark = pytest.mark.unit


def sample(*, tier: str = "A") -> Sample:
    return Sample.model_validate(
        {
            "sample_id": "a" * 24,
            "prepared_path": "prepared/" + "a" * 24 + ".png",
            "prepared_sha256": "b" * 64,
            "quality": {"tier": tier, "version": "quality-heuristic-v1"},
            "annotation": {
                "invoice": {
                    "seller_name": "Synthetic Supplier 001",
                    "invoice_number": "SYN-001",
                    "invoice_date": "09/23/2026",
                },
                "subtotal": {"tax": "20,00"},
                "items": [
                    {
                        "description": "Synthetic service",
                        "quantity": "2.00",
                        "total_price": "100,00",
                    }
                ],
            },
        }
    )


def test_perfect_field_match_normalizes_dates_decimals_and_text() -> None:
    counts = score_sample(sample(), invoice_extraction())
    assert set(counts) == {
        "vendor_name",
        "invoice_number",
        "invoice_date",
        "tax_amount",
        "line_description",
        "line_quantity",
        "line_total",
    }
    assert all(count.tp == 1 and count.fp == count.fn == 0 for count in counts.values())
    assert summarize([("A", counts)])["A"] == {
        "sample_count": 1,
        "overall": {
            "tp": 7,
            "fp": 0,
            "fn": 0,
            "eligible": 7,
            "precision": "1.0000",
            "recall": "1.0000",
            "f1": "1.0000",
        },
        "fields": {field: count.summary() for field, count in counts.items()},
    }


def test_comma_decimal_quantity_matches_extracted_decimal() -> None:
    entry = sample()
    annotation = entry.annotation.model_copy(
        update={"items": (entry.annotation.items[0].model_copy(update={"quantity": "2,00"}),)}
    )
    scores = score_sample(entry.model_copy(update={"annotation": annotation}), invoice_extraction())
    assert scores["line_quantity"].tp == 1


def test_wrong_values_and_extra_line_count_false_positives_and_negatives() -> None:
    original = invoice_extraction()
    changed = original.model_copy(
        update={
            "vendor_name": original.vendor_name.model_copy(update={"value": "Different supplier"}),
            "line_items": (*original.line_items, original.line_items[0]),
        }
    )
    counts = score_sample(sample(), changed)
    assert counts["vendor_name"].summary() == {
        "tp": 0,
        "fp": 1,
        "fn": 1,
        "eligible": 1,
        "precision": "0.0000",
        "recall": "0.0000",
        "f1": "0.0000",
    }
    assert counts["line_description"].tp == 1
    assert counts["line_description"].fp == 1
    assert counts["line_quantity"].fp == counts["line_total"].fp == 1


def test_escalation_counts_annotated_fields_as_misses_and_empty_tiers_as_unavailable() -> None:
    counts = score_sample(sample(), None)
    assert all(count.fn == 1 and count.tp == count.fp == 0 for count in counts.values())
    summary = summarize([("A", counts)])
    assert summary["B"] is None and summary["C"] is None
    assert summary["A"] is not None
    assert summary["A"]["overall"]["f1"] == "0.0000"


def test_manifest_rejects_duplicate_sample_identity() -> None:
    data = {
        "revision": "synthetic-revision",
        "metadata_sha256": "a" * 64,
        "pipeline_version": "voxel51-preparation-v1",
        "samples": [sample().model_dump(), sample().model_dump()],
    }
    with pytest.raises(ValidationError):
        Manifest.model_validate(data)
