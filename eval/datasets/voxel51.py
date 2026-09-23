"""Pinned synthetic-corpus selection and deterministic image preparation, without model calls."""

import hashlib
import io
import json
import warnings
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from time import monotonic
from typing import Final, Literal

import httpx
from PIL import Image, ImageOps, features
from PIL import __version__ as pillow_version
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from invoiceops_agent.artifacts import write_new_artifact

REPOSITORY = "Voxel51/high-quality-invoice-images-for-ocr"
REVISION = "d21f03cfeea2b330e15a229883c66d7ebece8e69"
METADATA_SHA256 = "79cb36846bf28f41636d795d2f85fd8b7ef99a8e9807f6ddc8e6f0d7c883ae7d"
BASE_URL = f"https://huggingface.co/datasets/{REPOSITORY}/resolve/{REVISION}"
MAX_METADATA_BYTES = 10_000_000
MAX_IMAGE_BYTES = 10_000_000
MAX_PIXELS = 30_000_000
PIPELINE_VERSION: Final = "voxel51-preparation-v1"
QUALITY_VERSION: Final = "quality-heuristic-v1"
Fetch = Callable[[str, int], bytes]


class PreparationError(Exception):
    """Input integrity, bounds, or an existing output prevents reproducible preparation."""


class CorpusId(BaseModel):
    value: str = Field(alias="$oid", pattern=r"^[0-9a-f]{24}$")


class CorpusSample(BaseModel):
    model_config = ConfigDict(extra="ignore")
    identity: CorpusId = Field(alias="_id")
    filepath: str = Field(pattern=r"^data/[A-Za-z0-9_.-]+\.jpg$")
    json_annotation: str | None = None


class Corpus(BaseModel):
    samples: list[CorpusSample] = Field(max_length=20_000)


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    count: int = Field(default=32, ge=1, le=500)
    seed: int = Field(default=20260827, ge=0, le=2**32 - 1)
    max_edge: int = Field(default=2400, ge=1000, le=4000)


class Quality(BaseModel):
    version: Literal["quality-heuristic-v1"] = QUALITY_VERSION
    tier: Literal["A", "B", "C"]
    short_edge: int
    contrast: int
    mean_gradient_x100: int


class PreparedSample(BaseModel):
    sample_id: str
    source_path: str
    source_sha256: str
    raw_path: str
    prepared_path: str
    prepared_sha256: str
    width: int
    height: int
    quality: Quality
    annotation: dict[str, JsonValue]


class Manifest(BaseModel):
    pipeline_version: Literal["voxel51-preparation-v1"] = PIPELINE_VERSION
    split: Literal["baseline-development"] = "baseline-development"
    repository: str = REPOSITORY
    revision: str = REVISION
    metadata_sha256: str = METADATA_SHA256
    source_records: int
    annotated_records: int
    unique_annotated_records: int
    duplicate_source_records: int
    settings: Settings
    pillow_version: str
    zlib_version: str | None
    samples: list[PreparedSample]


class Annotation(BaseModel):
    """Preserve upstream fields without inventing currency, unit prices, or missing labels."""

    model_config = ConfigDict(extra="forbid")
    invoice: dict[str, str]
    items: list[dict[str, str]] = Field(min_length=1, max_length=1000)
    subtotal: dict[str, str]
    payment_instructions: dict[str, str]


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fetch_bytes(url: str, limit: int) -> bytes:
    """Bound public downloads, including streamed responses without a Content-Length."""
    started = monotonic()
    with httpx.stream("GET", url, follow_redirects=True, timeout=30) as response:
        response.raise_for_status()
        chunks: list[bytes] = []
        size = 0
        for chunk in response.iter_bytes():
            if monotonic() - started > 60:
                raise PreparationError("Download exceeded its total time limit")
            size += len(chunk)
            if size > limit:
                raise PreparationError("Download exceeded its byte limit")
            chunks.append(chunk)
        return b"".join(chunks)


def select_samples(metadata: bytes, settings: Settings) -> tuple[Corpus, list[CorpusSample]]:
    if len(metadata) > MAX_METADATA_BYTES or digest(metadata) != METADATA_SHA256:
        raise PreparationError("Corpus metadata does not match the pinned checksum")
    corpus = Corpus.model_validate_json(metadata)
    identities = [sample.identity.value for sample in corpus.samples]
    if len(identities) != len(set(identities)):
        raise PreparationError("Corpus identities must be unique")
    annotated = unique_annotated_samples(corpus)
    if settings.count > len(annotated):
        raise PreparationError("Requested subset exceeds the annotated corpus")
    ranked = sorted(
        annotated,
        key=lambda sample: digest(f"{settings.seed}:{sample.identity.value}".encode("ascii")),
    )
    return corpus, ranked[: settings.count]


def unique_annotated_samples(corpus: Corpus) -> list[CorpusSample]:
    """Keep the smallest identity for repeated paths only when annotations agree exactly."""
    by_path: dict[str, CorpusSample] = {}
    for sample in sorted(corpus.samples, key=lambda row: row.identity.value):
        if not sample.json_annotation:
            continue
        previous = by_path.get(sample.filepath)
        if previous is None:
            by_path[sample.filepath] = sample
        elif previous.json_annotation != sample.json_annotation:
            raise PreparationError("A repeated source path has conflicting annotations")
    return list(by_path.values())


def assess_quality(image: Image.Image) -> Quality:
    """Versioned proxies for readability; these are not human-verified quality labels."""
    preview = ImageOps.grayscale(image)
    preview.thumbnail((512, 512), Image.Resampling.LANCZOS)
    values = preview.tobytes()
    histogram = preview.histogram()
    low_target, high_target = max(1, len(values) // 200), len(values) * 199 // 200
    cumulative = 0
    low = high = 0
    for intensity, count in enumerate(histogram):
        cumulative += count
        if cumulative < low_target:
            low = intensity + 1
        if cumulative < high_target:
            high = intensity + 1
    contrast = high - low
    width, height = preview.size
    horizontal = sum(
        abs(values[y * width + x] - values[y * width + x - 1])
        for y in range(height)
        for x in range(1, width)
    )
    vertical = sum(
        abs(values[y * width + x] - values[(y - 1) * width + x])
        for y in range(1, height)
        for x in range(width)
    )
    pairs = height * (width - 1) + width * (height - 1)
    gradient = (horizontal + vertical) * 100 // max(1, pairs)
    short_edge = min(image.size)
    tier: Literal["A", "B", "C"] = "C"
    if short_edge >= 1200 and contrast >= 128 and gradient >= 300:
        tier = "A"
    elif short_edge >= 700 and contrast >= 64 and gradient >= 100:
        tier = "B"
    return Quality(tier=tier, short_edge=short_edge, contrast=contrast, mean_gradient_x100=gradient)


def normalize_image(raw: bytes, settings: Settings) -> tuple[bytes, tuple[int, int], Quality]:
    if not raw or len(raw) > MAX_IMAGE_BYTES:
        raise PreparationError("Image exceeds the allowed byte range")
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(raw)) as source:
            if source.format != "JPEG" or source.width * source.height > MAX_PIXELS:
                raise PreparationError("Corpus image must be a bounded JPEG")
            source.load()
            image = ImageOps.exif_transpose(source).convert("RGB")
    quality = assess_quality(image)
    image.thumbnail((settings.max_edge, settings.max_edge), Image.Resampling.LANCZOS)
    # A fresh image omits EXIF, comments, and other source metadata from the PNG artifact.
    clean = Image.frombytes("RGB", image.size, image.tobytes())
    output = io.BytesIO()
    clean.save(output, format="PNG", optimize=False, compress_level=9)
    return output.getvalue(), image.size, quality


def _write_immutable(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        write_new_artifact(path, data)
    except FileExistsError:
        if path.read_bytes() != data:
            raise PreparationError(
                "Existing output differs; choose a fresh output directory"
            ) from None


def prepare(output: Path, settings: Settings, *, fetch: Fetch = fetch_bytes) -> Manifest:
    metadata = fetch(f"{BASE_URL}/samples.json", MAX_METADATA_BYTES)
    corpus, selected = select_samples(metadata, settings)
    prepared: list[PreparedSample] = []
    for sample in selected:
        assert sample.json_annotation is not None
        annotation = Annotation.model_validate_json(sample.json_annotation)
        raw = fetch(f"{BASE_URL}/{sample.filepath}", MAX_IMAGE_BYTES)
        normalized, size, quality = normalize_image(raw, settings)
        identity = sample.identity.value
        raw_path, image_path = f"raw/{identity}.jpg", f"prepared/{identity}.png"
        _write_immutable(output / raw_path, raw)
        _write_immutable(output / image_path, normalized)
        prepared.append(
            PreparedSample(
                sample_id=identity,
                source_path=sample.filepath,
                source_sha256=digest(raw),
                raw_path=raw_path,
                prepared_path=image_path,
                prepared_sha256=digest(normalized),
                width=size[0],
                height=size[1],
                quality=quality,
                annotation=annotation.model_dump(mode="json"),
            )
        )
    manifest = Manifest(
        source_records=len(corpus.samples),
        annotated_records=sum(bool(row.json_annotation) for row in corpus.samples),
        unique_annotated_records=len(unique_annotated_samples(corpus)),
        duplicate_source_records=len(corpus.samples)
        - len({row.filepath for row in corpus.samples}),
        metadata_sha256=digest(metadata),
        settings=settings,
        pillow_version=pillow_version,
        zlib_version=features.version("zlib"),
        samples=prepared,
    )
    content = json.dumps(manifest.model_dump(mode="json"), sort_keys=True, indent=2) + "\n"
    attribution = (
        "# Synthetic Voxel51 invoice subset\n\n"
        f"Source: https://huggingface.co/datasets/{REPOSITORY}\n\n"
        f"Pinned revision: {REVISION}\n\n"
        "The upstream dataset card declares ODbL and credits Osama Hosam Abdellatif's\n"
        "original Kaggle corpus and Harpreet Sahota's FiftyOne port.\n"
        "These are synthetic invoices. This development subset is not a held-out evaluation set.\n"
        "See manifest.json for source hashes, normalized hashes, labels, and toolchain versions.\n"
    )
    _write_immutable(output / "ATTRIBUTION.md", attribution.encode("utf-8"))
    _write_immutable(output / "manifest.json", content.encode("utf-8"))
    return manifest


def tier_counts(manifest: Manifest) -> dict[str, int]:
    counts = Counter(sample.quality.tier for sample in manifest.samples)
    return {tier: counts[tier] for tier in ("A", "B", "C")}
