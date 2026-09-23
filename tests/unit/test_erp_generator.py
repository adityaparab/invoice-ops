"""The ERP fixture and factual match labels reproduce without network or time."""

from collections import Counter
from decimal import Decimal
from pathlib import Path
from random import random

import pytest
from eval.datasets.prepare_erp import report_bytes

from invoiceops_agent.tools.erp_generator import fixture_sha256, generate_fixture

pytestmark = pytest.mark.unit
EXPECTED_DIGEST = "5d458199836517375ab1fefef03d6063d9622c60efbf8590d8aa2edfd5ec45c5"


def test_default_fixture_is_byte_identical_and_pinned() -> None:
    first = generate_fixture()
    random()
    second = generate_fixture()
    assert first.model_dump_json() == second.model_dump_json()
    assert fixture_sha256(first) == EXPECTED_DIGEST
    assert (len(first.vendors), len(first.purchase_orders), len(first.goods_receipts)) == (
        12,
        24,
        12,
    )
    assert generate_fixture(20260828).model_dump_json() != first.model_dump_json()
    assert report_bytes(first) == Path("eval/reports/synthetic-erp-v1.json").read_bytes()


def test_ground_truth_matches_order_and_receipt_records() -> None:
    fixture = generate_fixture()
    assert Counter(order.status for order in fixture.purchase_orders) == {
        "OPEN": 6,
        "PARTIALLY_RECEIVED": 6,
        "CLOSED": 6,
        "CANCELLED": 6,
    }
    vendors = {vendor.id: vendor for vendor in fixture.vendors}
    receipts = {receipt.purchase_order_id: receipt for receipt in fixture.goods_receipts}
    for order, truth in zip(fixture.purchase_orders, fixture.ground_truth, strict=True):
        assert truth.po_number == order.po_number
        assert truth.vendor_external_id == vendors[order.vendor_id].external_id
        assert truth.ordered_total == sum(
            (line.quantity * line.unit_price for line in order.lines), Decimal(0)
        )
        assert truth.ordered_quantities == tuple(line.quantity for line in order.lines)
        expected_received = (
            tuple(line.received_quantity for line in receipts[order.id].lines)
            if order.id in receipts
            else (Decimal(0),) * len(order.lines)
        )
        assert truth.received_quantities == expected_received
        assert truth.fully_received == (order.status == "CLOSED")
        assert all(line.sku.startswith("SYN-SKU-") for line in order.lines)
    assert all(vendor.name.startswith("Synthetic ") for vendor in fixture.vendors)
    assert all(vendor.bank_account_iban.startswith("GB00SYNTH") for vendor in fixture.vendors)


def test_invalid_seed_is_rejected() -> None:
    with pytest.raises(ValueError, match="32-bit"):
        generate_fixture(-1)
