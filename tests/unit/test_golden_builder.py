"""Golden labels must remain deterministic and agree with decision rules."""

import io
import json
from collections import Counter
from pathlib import Path
from random import Random
from uuid import UUID

import pytest
from eval.golden.builder import (
    ANOMALY_WEIGHTS,
    EFFECTS,
    GoldenBuildError,
    _near_duplicate,
    _synthetic_case,
    _upstream_money,
    render_invoice,
)
from eval.golden.schema import AnomalyCode, BaselineReport, GoldenERP, GoldenManifest, InvoiceLabel
from PIL import Image

from invoiceops_agent.schemas.exceptions import TaxonomyRequest
from invoiceops_agent.schemas.extraction import InvoiceExtraction
from invoiceops_agent.schemas.matching import ERPSnapshot, MatchRequest
from invoiceops_agent.tools.erp_generator import generate_fixture
from invoiceops_agent.tools.exception_taxonomy import classify_exceptions
from invoiceops_agent.tools.matching import match_invoice
from invoiceops_agent.tools.validation import validate_invoice

pytestmark = pytest.mark.unit


def _field(value: object) -> dict[str, object]:
    return {"value": value, "confidence": "0" if value is None else "1"}


def _extraction(label: InvoiceLabel) -> InvoiceExtraction:
    return InvoiceExtraction.model_validate(
        {
            "vendor_name": _field(label.vendor_name),
            "vendor_tax_id": _field(label.vendor_tax_id),
            "bank_account_iban": _field(label.bank_account_iban),
            "invoice_number": _field(label.invoice_number),
            "po_number": _field(label.po_number),
            "currency": _field(label.currency),
            "invoice_date": _field(label.invoice_date),
            "due_date": _field(label.due_date),
            "subtotal": _field(label.subtotal),
            "tax_amount": _field(label.tax_amount),
            "total_amount": _field(label.total_amount),
            "line_items": [
                {
                    "description": _field(line.description),
                    "quantity": _field(line.quantity),
                    "unit_price": _field(line.unit_price),
                    "tax_rate": _field(line.tax_rate),
                    "line_total": _field(line.line_total),
                }
                for line in label.line_items
            ],
        }
    )


def test_clean_case_passes_validation_and_three_way_match() -> None:
    vendor = generate_fixture().vendors[0]
    label, order, receipt = _synthetic_case("SYN-CLEAN-TEST", vendor, Random(9), None, 9)
    assert order is not None and receipt is not None
    extraction = _extraction(label)
    assert validate_invoice(extraction).status == "PASS"
    snapshot = ERPSnapshot(vendor=vendor, purchase_order=order, goods_receipts=(receipt,))
    match = match_invoice(
        MatchRequest(
            run_id=UUID(int=1),
            invoice_id=UUID(int=2),
            trace_id="0" * 32,
            extraction=extraction,
            snapshot=snapshot,
        )
    )
    assert match.status == "PASS"


@pytest.mark.parametrize(
    ("code", "result"),
    [
        ("PRICE_MM", "match"),
        ("QTY_MM", "match"),
        ("MISSING_PO", "match"),
        ("BANK_CHANGE", "bank"),
        ("CCY_MM", "match"),
        ("TAX_ERR", "validation"),
        ("MATH_ERR", "validation"),
        ("STALE_PO", "status"),
    ],
)
def test_anomaly_changes_its_intended_evidence(code: AnomalyCode, result: str) -> None:
    vendor = generate_fixture().vendors[0]
    label, order, receipt = _synthetic_case("SYN-ANOMALY-TEST", vendor, Random(9), code, 9)
    extraction = _extraction(label)
    validation = validate_invoice(extraction)
    snapshot = (
        ERPSnapshot(vendor=vendor, purchase_order=order, goods_receipts=(receipt,))
        if order is not None and receipt is not None
        else None
    )
    match = match_invoice(
        MatchRequest(
            run_id=UUID(int=1),
            invoice_id=UUID(int=2),
            trace_id="0" * 32,
            extraction=extraction,
            snapshot=snapshot,
        )
    )
    if result == "match":
        assert match.status != "PASS"
    elif result == "validation":
        assert validation.status == "FAIL"
    elif result == "bank":
        assert label.bank_account_iban != vendor.bank_account_iban
    else:
        assert order is not None and order.status == "CLOSED"


def test_rendered_artifacts_are_stable_and_distinct_for_near_duplicates() -> None:
    vendor = generate_fixture().vendors[0]
    label, _, _ = _synthetic_case("SYN-CLEAN-TEST", vendor, Random(9), None, 9)
    first = render_invoice(label)
    assert render_invoice(label) == first
    assert _near_duplicate(first) != first
    with Image.open(io.BytesIO(first)) as image:
        assert image.format == "PNG"
    assert len(EFFECTS) == 30
    assert sum(ANOMALY_WEIGHTS.values()) == 150


def test_invalid_upstream_money_is_rejected() -> None:
    with pytest.raises(GoldenBuildError):
        _upstream_money("not-money")


def test_committed_manifest_has_no_baseline_or_split_leakage() -> None:
    root = Path(__file__).resolve().parents[2]
    manifest = GoldenManifest.model_validate_json(
        (root / "eval/golden/v1.0.0/manifest.json").read_bytes()
    )
    erp = GoldenERP.model_validate_json((root / "eval/golden/v1.0.0/erp.json").read_bytes())
    baseline = BaselineReport.model_validate(
        json.loads((root / "eval/reports/voxel51-v1.json").read_text())
    )
    old_ids = {row.sample_id for row in baseline.samples}
    old_paths = {row.source_path for row in baseline.samples}
    old_hashes = {row.source_sha256 for row in baseline.samples}
    by_id = {sample.sample_id: sample for sample in manifest.samples}
    assert len(erp.purchase_orders) == len(erp.goods_receipts) == 399
    assert len({order.po_number for order in erp.purchase_orders}) == 399
    assert Counter(sample.origin for sample in manifest.samples) == {
        "voxel51": 50,
        "synthetic": 450,
    }
    assert Counter(code for sample in manifest.samples for code in sample.anomaly_codes) == {
        **ANOMALY_WEIGHTS
    }
    assert all(
        sample.source_id not in old_ids
        and sample.source_path not in old_paths
        and sample.source_sha256 not in old_hashes
        for sample in manifest.samples
        if sample.origin == "voxel51"
    )
    assert all(
        by_id[sample.parent_id].split == sample.split
        for sample in manifest.samples
        if sample.parent_id is not None
    )
    assert all(
        sample.routing_eligible == (sample.origin == "synthetic") for sample in manifest.samples
    )


def test_every_synthetic_clean_label_passes_deterministic_checks() -> None:
    root = Path(__file__).resolve().parents[2]
    manifest = GoldenManifest.model_validate_json(
        (root / "eval/golden/v1.0.0/manifest.json").read_bytes()
    )
    erp = GoldenERP.model_validate_json((root / "eval/golden/v1.0.0/erp.json").read_bytes())
    orders = {order.po_number: order for order in erp.purchase_orders}
    vendors = {vendor.id: vendor for vendor in erp.vendors}
    receipts = {receipt.purchase_order_id: receipt for receipt in erp.goods_receipts}
    for sample in manifest.samples:
        if sample.origin != "synthetic" or sample.anomaly_codes:
            continue
        label = sample.label
        assert label.po_number is not None
        order = orders[label.po_number]
        extraction = _extraction(label)
        assert validate_invoice(extraction).status == "PASS", sample.sample_id
        snapshot = ERPSnapshot(
            vendor=vendors[order.vendor_id],
            purchase_order=order,
            goods_receipts=(receipts[order.id],),
        )
        result = match_invoice(
            MatchRequest(
                run_id=UUID(int=1),
                invoice_id=UUID(int=2),
                trace_id="0" * 32,
                extraction=extraction,
                snapshot=snapshot,
            )
        )
        assert result.status == "PASS", sample.sample_id
        taxonomy = classify_exceptions(
            TaxonomyRequest(
                run_id=UUID(int=1),
                invoice_id=UUID(int=2),
                trace_id="0" * 32,
                extraction=extraction,
                validation=validate_invoice(extraction),
                match=result,
                vendor_bank_iban=snapshot.vendor.bank_account_iban,
            )
        )
        assert taxonomy.status == "CLEAN", sample.sample_id


def test_every_injected_code_is_supported_by_taxonomy_evidence() -> None:
    root = Path(__file__).resolve().parents[2]
    manifest = GoldenManifest.model_validate_json(
        (root / "eval/golden/v1.0.0/manifest.json").read_bytes()
    )
    erp = GoldenERP.model_validate_json((root / "eval/golden/v1.0.0/erp.json").read_bytes())
    orders = {order.po_number: order for order in erp.purchase_orders}
    vendors = {vendor.id: vendor for vendor in erp.vendors}
    receipts = {receipt.purchase_order_id: receipt for receipt in erp.goods_receipts}
    for sample in manifest.samples:
        if not sample.anomaly_codes:
            continue
        label = sample.label
        assert label.po_number is not None
        order = orders.get(label.po_number)
        snapshot = (
            ERPSnapshot(
                vendor=vendors[order.vendor_id],
                purchase_order=order,
                goods_receipts=(receipts[order.id],),
            )
            if order is not None
            else None
        )
        extraction = _extraction(label)
        validation = validate_invoice(extraction)
        match = match_invoice(
            MatchRequest(
                run_id=UUID(int=1),
                invoice_id=UUID(int=2),
                trace_id="0" * 32,
                extraction=extraction,
                snapshot=snapshot,
            )
        )
        taxonomy = classify_exceptions(
            TaxonomyRequest(
                run_id=UUID(int=1),
                invoice_id=UUID(int=2),
                trace_id="0" * 32,
                extraction=extraction,
                validation=validation,
                match=match,
                vendor_bank_iban=snapshot.vendor.bank_account_iban if snapshot else None,
                exact_duplicate="DUP_EXACT" in sample.anomaly_codes,
                near_duplicate="DUP_NEAR" in sample.anomaly_codes,
                stale_po=order.status == "CLOSED" if order else False,
            )
        )
        assert set(sample.anomaly_codes) <= set(taxonomy.codes), sample.sample_id
