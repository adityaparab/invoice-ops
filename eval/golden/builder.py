"""Build a pinned synthetic golden set without model calls or private documents."""

import io
import json
import logging
from collections import Counter
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from random import Random
from uuid import NAMESPACE_URL, uuid5

from PIL import Image, ImageDraw, ImageFont, features
from PIL import __version__ as pillow_version

from eval.datasets.voxel51 import (
    BASE_URL,
    MAX_IMAGE_BYTES,
    MAX_METADATA_BYTES,
    METADATA_SHA256,
    REVISION,
    Annotation,
    Corpus,
    CorpusSample,
    Settings,
    assess_quality,
    digest,
    fetch_bytes,
    normalize_image,
    unique_annotated_samples,
)
from eval.golden.schema import (
    AnomalyCode,
    BaselineReport,
    GoldenERP,
    GoldenManifest,
    GoldenSample,
    InvoiceLabel,
    LineLabel,
    QualityEffect,
    Split,
)
from invoiceops_agent.artifacts import write_new_artifact
from invoiceops_agent.schemas.erp import (
    GoodsReceipt,
    PurchaseOrder,
    PurchaseOrderLine,
    ReceiptLine,
    Vendor,
)
from invoiceops_agent.tools.erp_generator import generate_fixture

logger = logging.getLogger(__name__)
VERSION = "golden-builder@v1"
SEED = 20260827
VOXEL_COUNT = 50
SYNTHETIC_CLEAN_COUNT = 300
ANOMALY_WEIGHTS: dict[AnomalyCode, int] = {
    "DUP_EXACT": 20,
    "DUP_NEAR": 15,
    "PRICE_MM": 22,
    "QTY_MM": 20,
    "MISSING_PO": 16,
    "BANK_CHANGE": 10,
    "CCY_MM": 10,
    "TAX_ERR": 16,
    "MATH_ERR": 11,
    "STALE_PO": 10,
}
EFFECTS: tuple[QualityEffect, ...] = (
    ("rotation",) * 8 + ("skew",) * 8 + ("stamp",) * 7 + ("faint_print",) * 7
)
Fetch = Callable[[str, int], bytes]


class GoldenBuildError(Exception):
    """A source, integrity, or immutable-output error stopped dataset preparation."""


def _write_immutable(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        write_new_artifact(path, body)
    except FileExistsError:
        if path.read_bytes() != body:
            raise GoldenBuildError("Existing golden artifact differs") from None


def _rank(seed: int, value: str) -> str:
    return digest(f"{seed}:{value}".encode("ascii"))


def _money(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.01")), ".2f")


def _upstream_money(value: str) -> str:
    compact = value.strip().replace(",", "").replace("$", "").replace("€", "")
    try:
        return _money(Decimal(compact))
    except InvalidOperation as error:
        raise GoldenBuildError("Invalid upstream money label") from error


def relabel_voxel(sample: CorpusSample) -> InvoiceLabel:
    """Map only annotated values; absent PO, IBAN, currency, and price stay unknown."""
    if sample.json_annotation is None:
        raise GoldenBuildError("Unannotated source sample")
    source = Annotation.model_validate_json(sample.json_annotation)
    invoice = source.invoice
    try:
        invoice_date = datetime.strptime(invoice["invoice_date"], "%m/%d/%Y").date()
        lines = tuple(
            LineLabel(
                description=item["description"].strip(),
                quantity=format(Decimal(item["quantity"]).normalize(), "f"),
                line_total=_upstream_money(item["total_price"]),
            )
            for item in source.items
        )
        tax = source.subtotal.get("tax", "").strip()
        return InvoiceLabel(
            vendor_name=invoice["seller_name"].strip(),
            invoice_number=invoice["invoice_number"].strip(),
            invoice_date=invoice_date,
            tax_amount=_upstream_money(tax) if tax else None,
            line_items=lines,
        )
    except (KeyError, InvalidOperation, ValueError) as error:
        raise GoldenBuildError("Invalid upstream extraction label") from error


def select_voxel_sources(
    metadata: bytes, baseline_report: BaselineReport, seed: int
) -> tuple[CorpusSample, ...]:
    if len(metadata) > MAX_METADATA_BYTES or digest(metadata) != METADATA_SHA256:
        raise GoldenBuildError("Pinned corpus metadata checksum failed")
    corpus = Corpus.model_validate_json(metadata)
    excluded_ids = {row.sample_id for row in baseline_report.samples}
    excluded_paths = {row.source_path for row in baseline_report.samples}
    candidates = sorted(
        unique_annotated_samples(corpus), key=lambda row: _rank(seed, row.identity.value)
    )
    selected: list[CorpusSample] = []
    for candidate in candidates:
        if candidate.identity.value in excluded_ids or candidate.filepath in excluded_paths:
            continue
        try:
            relabel_voxel(candidate)
        except GoldenBuildError:
            continue
        selected.append(candidate)
        if len(selected) == VOXEL_COUNT:
            break
    if len(selected) != VOXEL_COUNT:
        raise GoldenBuildError("Insufficient eligible held-out Voxel51 invoices")
    return tuple(selected)


def _split_ids(ids: list[str], count: int, seed: int) -> set[str]:
    return set(sorted(ids, key=lambda value: _rank(seed, value))[:count])


def _case_id(kind: str, index: int) -> str:
    return f"SYN-{kind}-{index:04d}"


def _timestamp(day: date) -> datetime:
    return datetime.combine(day, time(12), tzinfo=UTC)


def _synthetic_case(
    sample_id: str,
    vendor: Vendor,
    rng: Random,
    code: AnomalyCode | None,
    seed: int,
) -> tuple[InvoiceLabel, PurchaseOrder | None, GoodsReceipt | None]:
    quantity = Decimal(rng.randint(2, 8))
    unit_price = Decimal(rng.randint(500, 8000)) / 100
    base_quantity = quantity
    base_price = unit_price
    currency = ("EUR", "USD", "GBP")[rng.randrange(3)]
    base_currency = currency
    invoice_date = date(2026, 8, 27)
    po_number = f"SYN-GOLD-{seed}-PO-{sample_id}"
    bank = vendor.bank_account_iban
    status: str = "PARTIALLY_RECEIVED"
    if code == "PRICE_MM":
        unit_price += Decimal("5.00")
    elif code == "QTY_MM":
        quantity += Decimal(1)
    elif code == "MISSING_PO":
        po_number = f"SYN-GOLD-{seed}-MISSING-{sample_id}"
    elif code == "BANK_CHANGE":
        bank = f"GB00SYNTH{seed:010d}9999"
    elif code == "CCY_MM":
        currency = {"EUR": "USD", "USD": "GBP", "GBP": "EUR"}[currency]
    elif code == "STALE_PO":
        status = "CLOSED"
    line_total = quantity * unit_price
    subtotal = line_total
    tax = (subtotal * Decimal("0.20")).quantize(Decimal("0.01"))
    if code == "TAX_ERR":
        tax += Decimal("7.00")
    if code == "MATH_ERR":
        line_total += Decimal("7.00")
    total = subtotal + tax
    label = InvoiceLabel(
        vendor_name=vendor.name,
        vendor_tax_id=vendor.tax_id,
        bank_account_iban=bank,
        invoice_number=f"SYN-GOLD-{seed}-INV-{sample_id}",
        po_number=po_number,
        currency=currency,
        invoice_date=invoice_date,
        due_date=invoice_date + timedelta(days=30),
        subtotal=_money(subtotal),
        tax_amount=_money(tax),
        total_amount=_money(total),
        line_items=(
            LineLabel(
                description=f"Synthetic office supply {sample_id}",
                quantity=format(quantity, "f"),
                unit_price=_money(unit_price),
                tax_rate="0.20",
                line_total=_money(line_total),
            ),
        ),
    )
    if code == "MISSING_PO":
        return label, None, None
    po_id = uuid5(NAMESPACE_URL, f"golden:{seed}:po:{sample_id}")
    issued_on = invoice_date - timedelta(days=14)
    ordered = base_quantity * 2
    po = PurchaseOrder(
        id=po_id,
        vendor_id=vendor.id,
        po_number=po_number,
        status=status,
        currency=base_currency,
        issued_on=issued_on,
        total_amount=ordered * base_price,
        lines=(
            PurchaseOrderLine(
                line_number=1,
                sku=f"SYN-GOLD-SKU-{sample_id}",
                description=f"Synthetic office supply {sample_id}",
                quantity=ordered,
                unit_price=base_price,
                line_total=ordered * base_price,
            ),
        ),
        created_at=_timestamp(issued_on),
    )
    receipt = GoodsReceipt(
        id=uuid5(NAMESPACE_URL, f"golden:{seed}:receipt:{sample_id}"),
        purchase_order_id=po_id,
        receipt_number=f"SYN-GOLD-{seed}-GR-{sample_id}",
        received_at=_timestamp(issued_on + timedelta(days=7)),
        lines=(
            ReceiptLine(
                line_number=1,
                sku=f"SYN-GOLD-SKU-{sample_id}",
                received_quantity=base_quantity,
            ),
        ),
        created_at=_timestamp(issued_on + timedelta(days=7)),
    )
    return label, po, receipt


def render_invoice(label: InvoiceLabel, effect: QualityEffect | None = None) -> bytes:
    """Render a document from labels, then apply one deterministic quality stressor."""
    image = Image.new("RGB", (1200, 1500), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=32)
    title = ImageFont.load_default(size=52)
    draw.text((70, 55), "SYNTHETIC INVOICE", fill="black", font=title)
    rows = (
        ("Vendor", label.vendor_name),
        ("Tax ID", label.vendor_tax_id),
        ("Bank IBAN", label.bank_account_iban),
        ("Invoice", label.invoice_number),
        ("PO", label.po_number),
        ("Currency", label.currency),
        ("Date", label.invoice_date.isoformat() if label.invoice_date else None),
        ("Due", label.due_date.isoformat() if label.due_date else None),
    )
    y = 150
    for name, value in rows:
        draw.text((70, y), f"{name}: {value or ''}", fill="black", font=font)
        y += 65
    y += 45
    for line in label.line_items:
        draw.text((70, y), line.description, fill="black", font=font)
        y += 55
        draw.text(
            (70, y),
            f"Qty {line.quantity} x {line.unit_price} = {line.line_total}   Tax {line.tax_rate}",
            fill="black",
            font=font,
        )
        y += 65
    y += 60
    for name, value in (
        ("Subtotal", label.subtotal),
        ("Tax", label.tax_amount),
        ("TOTAL", label.total_amount),
    ):
        draw.text((70, y), f"{name}: {value or ''}", fill="black", font=font)
        y += 65
    if effect == "rotation":
        image = image.rotate(3, resample=Image.Resampling.BICUBIC, fillcolor="white")
    elif effect == "skew":
        image = image.transform(
            image.size,
            Image.Transform.AFFINE,
            (1, -0.04, 40, 0, 1, 0),
            resample=Image.Resampling.BICUBIC,
            fillcolor="white",
        )
    elif effect == "stamp":
        ImageDraw.Draw(image).text((730, 1200), "RECEIVED", fill=(175, 60, 60), font=title)
    elif effect == "faint_print":
        image = Image.blend(image, Image.new("RGB", image.size, "white"), 0.65)
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=False, compress_level=9)
    return output.getvalue()


def _near_duplicate(body: bytes) -> bytes:
    with Image.open(io.BytesIO(body)) as original:
        image = original.convert("RGB")
    ImageDraw.Draw(image).rectangle((1150, 1450, 1160, 1460), fill="black")
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=False, compress_level=9)
    return output.getvalue()


def _sample(
    sample_id: str,
    split: Split,
    body: bytes,
    label: InvoiceLabel,
    *,
    code: AnomalyCode | None = None,
    parent_id: str | None = None,
    effect: QualityEffect | None = None,
) -> GoldenSample:
    with Image.open(io.BytesIO(body)) as image:
        quality = assess_quality(image)
    return GoldenSample(
        sample_id=sample_id,
        split=split,
        origin="synthetic",
        document_path=f"documents/{sample_id}.png",
        document_sha256=digest(body),
        parent_id=parent_id,
        effect=effect,
        quality_tier=quality.tier,
        label=label,
        anomaly_codes=(code,) if code else (),
        routing_eligible=True,
    )


def _build_synthetic(
    output: Path, seed: int, vendors: tuple[Vendor, ...]
) -> tuple[list[GoldenSample], list[PurchaseOrder], list[GoodsReceipt]]:
    rng = Random(seed)
    clean_ids = [_case_id("CLEAN", index) for index in range(1, SYNTHETIC_CLEAN_COUNT + 1)]
    hard_ids = set(clean_ids[:30])
    clean_development = _split_ids(clean_ids[:30], 6, seed) | _split_ids(clean_ids[30:], 54, seed)
    samples: list[GoldenSample] = []
    orders: list[PurchaseOrder] = []
    receipts: list[GoodsReceipt] = []
    clean_lookup: dict[str, tuple[GoldenSample, bytes]] = {}
    for index, sample_id in enumerate(clean_ids):
        split: Split = "development" if sample_id in clean_development else "held_out"
        effect = EFFECTS[index] if sample_id in hard_ids else None
        label, order, receipt = _synthetic_case(
            sample_id, vendors[index % len(vendors)], rng, None, seed
        )
        assert order is not None and receipt is not None
        body = render_invoice(label, effect)
        sample = _sample(sample_id, split, body, label, effect=effect)
        _write_immutable(output / sample.document_path, body)
        samples.append(sample)
        orders.append(order)
        receipts.append(receipt)
        clean_lookup[sample_id] = sample, body
    for code, count in ANOMALY_WEIGHTS.items():
        ids = [_case_id(code, index) for index in range(1, count + 1)]
        development = _split_ids(ids, 3, seed)
        for index, sample_id in enumerate(ids):
            split = "development" if sample_id in development else "held_out"
            if code in {"DUP_EXACT", "DUP_NEAR"}:
                parents = [value for value in clean_ids if clean_lookup[value][0].split == split]
                parent_id = parents[index % len(parents)]
                parent, parent_body = clean_lookup[parent_id]
                body = parent_body if code == "DUP_EXACT" else _near_duplicate(parent_body)
                sample = _sample(
                    sample_id, split, body, parent.label, code=code, parent_id=parent_id
                )
            else:
                label, order, receipt = _synthetic_case(
                    sample_id,
                    vendors[(index + count) % len(vendors)],
                    rng,
                    code,
                    seed,
                )
                body = render_invoice(label)
                sample = _sample(sample_id, split, body, label, code=code)
                if order is not None and receipt is not None:
                    orders.append(order)
                    receipts.append(receipt)
            _write_immutable(output / sample.document_path, body)
            samples.append(sample)
    return samples, orders, receipts


def build(
    output: Path,
    metadata: bytes,
    baseline_report: BaselineReport,
    *,
    seed: int = SEED,
    fetch: Fetch = fetch_bytes,
) -> tuple[GoldenManifest, GoldenERP]:
    """Rebuild or verify all document bytes before publishing immutable manifests."""
    if seed < 0 or seed > 2**32 - 1:
        raise GoldenBuildError("Seed must be a 32-bit unsigned integer")
    source_rows = select_voxel_sources(metadata, baseline_report, seed)
    voxel_development = _split_ids([row.identity.value for row in source_rows], 10, seed)
    samples: list[GoldenSample] = []
    baseline_hashes = {row.source_sha256 for row in baseline_report.samples}
    settings = Settings(count=VOXEL_COUNT, seed=seed)
    for row in source_rows:
        raw = fetch(f"{BASE_URL}/{row.filepath}", MAX_IMAGE_BYTES)
        if digest(raw) in baseline_hashes:
            raise GoldenBuildError("Golden source overlaps the development baseline")
        body, _, quality = normalize_image(raw, settings)
        sample_id = f"VX-{row.identity.value}"
        path = f"documents/{sample_id}.png"
        _write_immutable(output / path, body)
        samples.append(
            GoldenSample(
                sample_id=sample_id,
                split="development" if row.identity.value in voxel_development else "held_out",
                origin="voxel51",
                document_path=path,
                document_sha256=digest(body),
                source_id=row.identity.value,
                source_path=row.filepath,
                source_sha256=digest(raw),
                quality_tier=quality.tier,
                label=relabel_voxel(row),
                routing_eligible=False,
            )
        )
    fixture = generate_fixture(seed)
    synthetic, orders, receipts = _build_synthetic(output, seed, fixture.vendors)
    samples.extend(synthetic)
    manifest = GoldenManifest(
        seed=seed,
        source_revision=REVISION,
        source_metadata_sha256=METADATA_SHA256,
        baseline_manifest_sha256=baseline_report.manifest_sha256,
        pillow_version=pillow_version,
        zlib_version=features.version("zlib"),
        samples=tuple(samples),
    )
    erp = GoldenERP(
        seed=seed,
        vendors=fixture.vendors,
        purchase_orders=tuple(orders),
        goods_receipts=tuple(receipts),
    )
    _write_immutable(
        output / "manifest.json",
        (json.dumps(manifest.model_dump(mode="json"), sort_keys=True, indent=2) + "\n").encode(),
    )
    _write_immutable(
        output / "erp.json",
        (json.dumps(erp.model_dump(mode="json"), sort_keys=True, indent=2) + "\n").encode(),
    )
    logger.info(
        "golden_dataset_built seed=%d samples=%d anomalies=%s",
        seed,
        len(samples),
        dict(Counter(code for sample in samples for code in sample.anomaly_codes)),
    )
    return manifest, erp
