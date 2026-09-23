"""Offline reproducibility, integrity, and image-boundary tests for the public corpus tool."""

import io
import json
from pathlib import Path
from typing import Literal

import pytest
from eval.datasets import voxel51
from PIL import Image, ImageDraw
from pydantic import ValidationError

pytestmark = pytest.mark.unit


def _metadata() -> bytes:
    annotation = json.dumps(
        {
            "invoice": {"seller_name": "Synthetic Example", "invoice_number": "DEMO-1"},
            "items": [{"quantity": "2.00", "total_price": "10.00"}],
            "subtotal": {"tax": "1.00", "total": "10.00"},
            "payment_instructions": {},
        }
    )
    return json.dumps(
        {
            "samples": [
                {
                    "_id": {"$oid": f"{n:024x}"},
                    "filepath": f"data/demo-{n}.jpg",
                    "json_annotation": annotation if n < 4 else None,
                }
                for n in range(1, 5)
            ]
        }
    ).encode()


def _jpeg(*, oriented: bool = False) -> bytes:
    image = Image.new("RGB", (1400, 1800), "white")
    draw = ImageDraw.Draw(image)
    for y in range(40, 1750, 24):
        draw.rectangle((40, y, 1360, y + 7), fill="black")
    exif = Image.Exif()
    if oriented:
        exif[274] = 6
    output = io.BytesIO()
    image.save(output, format="JPEG", exif=exif)
    return output.getvalue()


@pytest.fixture
def metadata(monkeypatch: pytest.MonkeyPatch) -> bytes:
    content = _metadata()
    monkeypatch.setattr(voxel51, "METADATA_SHA256", voxel51.digest(content))
    return content


def test_selection_is_seeded_and_uses_only_annotated_unique_samples(metadata: bytes) -> None:
    settings = voxel51.Settings(count=2)
    corpus, selected = voxel51.select_samples(metadata, settings)
    assert len(corpus.samples) == 4
    assert len(selected) == 2
    assert all(sample.json_annotation for sample in selected)
    assert selected == voxel51.select_samples(metadata, settings)[1]
    orders = {
        tuple(
            sample.identity.value
            for sample in voxel51.select_samples(metadata, voxel51.Settings(count=2, seed=seed))[1]
        )
        for seed in range(10)
    }
    assert len(orders) > 1


def test_corrupt_metadata_and_insufficient_annotation_coverage_fail(metadata: bytes) -> None:
    with pytest.raises(voxel51.PreparationError, match="checksum"):
        voxel51.select_samples(metadata + b" ", voxel51.Settings(count=1))
    with pytest.raises(voxel51.PreparationError, match="annotated corpus"):
        voxel51.select_samples(metadata, voxel51.Settings(count=4))


@pytest.mark.parametrize("path", ["../../outside.jpg", "data/../outside.jpg", "https://evil/a.jpg"])
def test_source_paths_cannot_escape_pinned_repository(path: str) -> None:
    with pytest.raises(ValidationError):
        voxel51.CorpusSample.model_validate({"_id": {"$oid": "1" * 24}, "filepath": path})


def test_duplicate_source_identity_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    doc = json.loads(_metadata())
    doc["samples"].append(doc["samples"][0])
    content = json.dumps(doc).encode()
    monkeypatch.setattr(voxel51, "METADATA_SHA256", voxel51.digest(content))
    with pytest.raises(voxel51.PreparationError, match="unique"):
        voxel51.select_samples(content, voxel51.Settings(count=1))


def test_normalization_is_deterministic_bounded_and_removes_exif() -> None:
    raw = _jpeg(oriented=True)
    settings = voxel51.Settings(max_edge=1000)
    first, size, quality = voxel51.normalize_image(raw, settings)
    assert (first, size, quality) == voxel51.normalize_image(raw, settings)
    assert size == (1000, 778)
    with Image.open(io.BytesIO(first)) as image:
        assert image.format == "PNG"
        assert image.mode == "RGB"
        assert not image.getexif()
    assert quality.tier == "A"


@pytest.mark.parametrize("kind", ["png", "pixels", "bytes"])
def test_oversized_or_wrong_format_images_are_rejected(
    monkeypatch: pytest.MonkeyPatch, kind: Literal["png", "pixels", "bytes"]
) -> None:
    raw = _jpeg()
    if kind == "png":
        stream = io.BytesIO()
        Image.new("RGB", (10, 10)).save(stream, format="PNG")
        raw = stream.getvalue()
    elif kind == "pixels":
        monkeypatch.setattr(voxel51, "MAX_PIXELS", 100)
    else:
        monkeypatch.setattr(voxel51, "MAX_IMAGE_BYTES", 100)
    with pytest.raises(voxel51.PreparationError):
        voxel51.normalize_image(raw, voxel51.Settings())


def test_quality_flags_blank_and_low_resolution_documents() -> None:
    assert voxel51.assess_quality(Image.new("RGB", (1400, 1800), "white")).tier == "C"
    with Image.open(io.BytesIO(_jpeg())) as source:
        source.thumbnail((300, 300))
        assert voxel51.assess_quality(source).tier == "C"


def test_prepare_is_reproducible_and_preserves_raw_labels(metadata: bytes, tmp_path: Path) -> None:
    raw = _jpeg()
    calls: list[str] = []

    def fetch(url: str, limit: int) -> bytes:
        calls.append(url)
        assert url.startswith(voxel51.BASE_URL)
        data = metadata if url.endswith("samples.json") else raw
        assert len(data) < limit
        return data

    settings = voxel51.Settings(count=2)
    first = voxel51.prepare(tmp_path, settings, fetch=fetch)
    manifest_bytes = (tmp_path / "manifest.json").read_bytes()
    second = voxel51.prepare(tmp_path, settings, fetch=fetch)
    assert first == second
    assert (tmp_path / "manifest.json").read_bytes() == manifest_bytes
    assert first.annotated_records == 3
    assert first.samples[0].annotation["subtotal"] == {"tax": "1.00", "total": "10.00"}
    assert len(calls) == 6
    assert sum(voxel51.tier_counts(first).values()) == 2
    artifact = tmp_path / first.samples[0].prepared_path
    artifact.write_bytes(b"existing-user-content")
    with pytest.raises(voxel51.PreparationError, match="Existing output differs"):
        voxel51.prepare(tmp_path, settings, fetch=fetch)
    assert artifact.read_bytes() == b"existing-user-content"


def test_identical_duplicate_source_is_selected_only_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    doc = json.loads(_metadata())
    duplicate = dict(doc["samples"][0])
    duplicate["_id"] = {"$oid": "e" * 24}
    doc["samples"].append(duplicate)
    content = json.dumps(doc).encode()
    monkeypatch.setattr(voxel51, "METADATA_SHA256", voxel51.digest(content))
    _, selected = voxel51.select_samples(content, voxel51.Settings(count=3))
    assert len(selected) == 3
    assert len({row.filepath for row in selected}) == 3
    assert "e" * 24 not in {row.identity.value for row in selected}


def test_conflicting_duplicate_annotations_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    doc = json.loads(_metadata())
    duplicate = dict(doc["samples"][0])
    duplicate["_id"] = {"$oid": "e" * 24}
    duplicate["json_annotation"] = "{}"
    doc["samples"].append(duplicate)
    content = json.dumps(doc).encode()
    monkeypatch.setattr(voxel51, "METADATA_SHA256", voxel51.digest(content))
    with pytest.raises(voxel51.PreparationError, match="conflicting annotations"):
        voxel51.select_samples(content, voxel51.Settings(count=3))


def test_medium_resolution_is_distinct_from_high_and_low_quality() -> None:
    with Image.open(io.BytesIO(_jpeg())) as image:
        image.thumbnail((1100, 1100))
        assert voxel51.assess_quality(image).tier == "B"


@pytest.mark.parametrize("failure", ["size", "elapsed", "status"])
def test_public_download_is_bounded_without_network(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    import httpx

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503 if failure == "status" else 200, content=b"x" * 128)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(httpx, "stream", client.stream)
        if failure == "elapsed":
            ticks = iter([0.0, 61.0])
            monkeypatch.setattr(voxel51, "monotonic", lambda: next(ticks))
        with pytest.raises(
            httpx.HTTPStatusError if failure == "status" else voxel51.PreparationError
        ):
            voxel51.fetch_bytes("https://dataset.invalid/test", 16 if failure == "size" else 1024)


def test_failed_disk_publication_leaves_no_partial_final_and_can_resume(
    metadata: bytes, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    raw = _jpeg()

    def fetch(url: str, limit: int) -> bytes:
        return metadata if url.endswith("samples.json") else raw

    original_fsync = os.fsync

    def fail_sync(fd: int) -> None:
        raise OSError("injected disk failure")

    monkeypatch.setattr(os, "fsync", fail_sync)
    with pytest.raises(OSError, match="injected disk failure"):
        voxel51.prepare(tmp_path, voxel51.Settings(count=1), fetch=fetch)
    assert not list(tmp_path.rglob("*.jpg"))
    assert not (tmp_path / "manifest.json").exists()
    monkeypatch.setattr(os, "fsync", original_fsync)
    manifest = voxel51.prepare(tmp_path, voxel51.Settings(count=1), fetch=fetch)
    assert len(manifest.samples) == 1
    assert (tmp_path / "manifest.json").is_file()
