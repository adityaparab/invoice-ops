"""Pure, seed-pinned Faker generator for the synthetic ERP fixture."""

import hashlib
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from random import Random
from uuid import NAMESPACE_URL, UUID, uuid5

from faker import Faker

from invoiceops_agent.schemas.erp import (
    ERPFixture,
    GoodsReceipt,
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseOrderTruth,
    ReceiptLine,
    Vendor,
)

GENERATOR_VERSION = "synthetic-erp@v1"
DEFAULT_SEED = 20260827
AS_OF = date(2026, 8, 27)
VENDOR_COUNT = 12
ORDERS_PER_VENDOR = 2
CURRENCIES = ("EUR", "USD", "GBP")
STATUSES = ("OPEN", "PARTIALLY_RECEIVED", "CLOSED", "CANCELLED")


def _id(seed: int, entity: str, index: int) -> UUID:
    return uuid5(NAMESPACE_URL, f"invoiceops:{GENERATOR_VERSION}:{seed}:{entity}:{index}")


def _amount(cents: int) -> Decimal:
    return Decimal(cents) / Decimal(100)


def _timestamp(day: date) -> datetime:
    return datetime.combine(day, time(12), tzinfo=UTC)


def generate_fixture(seed: int = DEFAULT_SEED) -> ERPFixture:
    """Return the same typed records and ground truth for a given seed."""
    if seed < 0 or seed > 2**32 - 1:
        raise ValueError("ERP seed must be a 32-bit unsigned integer")
    rng = Random(seed)
    fake = Faker("en_US")
    fake.seed_instance(seed)
    vendors: list[Vendor] = []
    orders: list[PurchaseOrder] = []
    receipts: list[GoodsReceipt] = []
    truth: list[PurchaseOrderTruth] = []
    for vendor_index in range(1, VENDOR_COUNT + 1):
        vendor = Vendor(
            id=_id(seed, "vendor", vendor_index),
            external_id=f"SYN-{seed}-V{vendor_index:03d}",
            name=f"Synthetic {fake.color_name()} {fake.word().title()} Supplies {vendor_index:03d}",
            tax_id=f"SYN-TAX-{seed}-{vendor_index:03d}",
            # Deliberately invalid check digits: this is never a payable bank account.
            bank_account_iban=f"GB00SYNTH{seed:010d}{vendor_index:04d}",
            created_at=_timestamp(AS_OF),
        )
        vendors.append(vendor)
        for within_vendor in range(ORDERS_PER_VENDOR):
            order_index = (vendor_index - 1) * ORDERS_PER_VENDOR + within_vendor + 1
            status = STATUSES[(order_index - 1) % len(STATUSES)]
            issued_on = AS_OF - timedelta(days=rng.randint(14, 90))
            lines: list[PurchaseOrderLine] = []
            for line_number in range(1, rng.randint(2, 4) + 1):
                quantity = Decimal(rng.randint(2, 12))
                unit_price = _amount(rng.randint(500, 25_000))
                lines.append(
                    PurchaseOrderLine(
                        line_number=line_number,
                        sku=f"SYN-SKU-{order_index:03d}-{line_number:02d}",
                        description=f"Synthetic {fake.word().title()} item {line_number}",
                        quantity=quantity,
                        unit_price=unit_price,
                        line_total=quantity * unit_price,
                    )
                )
            order = PurchaseOrder(
                id=_id(seed, "order", order_index),
                vendor_id=vendor.id,
                po_number=f"SYN-{seed}-PO{order_index:04d}",
                status=status,
                currency=CURRENCIES[(order_index - 1) % len(CURRENCIES)],
                issued_on=issued_on,
                total_amount=sum((line.line_total for line in lines), Decimal(0)),
                lines=tuple(lines),
                created_at=_timestamp(issued_on),
            )
            orders.append(order)
            received_quantities = tuple(
                line.quantity // 2
                if status == "PARTIALLY_RECEIVED"
                else line.quantity
                if status == "CLOSED"
                else Decimal(0)
                for line in lines
            )
            if status in {"PARTIALLY_RECEIVED", "CLOSED"}:
                received_at = _timestamp(issued_on + timedelta(days=7))
                receipts.append(
                    GoodsReceipt(
                        id=_id(seed, "receipt", order_index),
                        purchase_order_id=order.id,
                        receipt_number=f"SYN-{seed}-GR{order_index:04d}",
                        received_at=received_at,
                        lines=tuple(
                            ReceiptLine(
                                line_number=line.line_number,
                                sku=line.sku,
                                received_quantity=received,
                            )
                            for line, received in zip(lines, received_quantities, strict=True)
                        ),
                        created_at=received_at,
                    )
                )
            truth.append(
                PurchaseOrderTruth(
                    po_number=order.po_number,
                    vendor_external_id=vendor.external_id,
                    status=order.status,
                    ordered_total=order.total_amount,
                    ordered_quantities=tuple(line.quantity for line in lines),
                    received_quantities=received_quantities,
                    fully_received=all(
                        line.quantity == received
                        for line, received in zip(lines, received_quantities, strict=True)
                    ),
                )
            )
    return ERPFixture(
        seed=seed,
        vendors=tuple(vendors),
        purchase_orders=tuple(orders),
        goods_receipts=tuple(receipts),
        ground_truth=tuple(truth),
    )


def fixture_sha256(fixture: ERPFixture) -> str:
    return hashlib.sha256(fixture.model_dump_json().encode("utf-8")).hexdigest()
