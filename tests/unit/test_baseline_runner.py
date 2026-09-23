"""The baseline uses the real agent and gateway contract with offline SDK responses."""

import hashlib
import json
from pathlib import Path

import httpx2
import pytest
from eval.baseline.run import run_baseline
from eval.baseline.settings import BaselineSettings
from tests.unit.extraction_support import png_bytes
from tests.unit.test_baseline_metrics import sample
from tests.unit.test_extraction_agent import gateway_settings, model_response

from invoiceops_agent.gateway_client import GatewayClient

pytestmark = pytest.mark.unit


def dataset(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "data"
    prepared = root / "prepared"
    prepared.mkdir(parents=True)
    body = png_bytes()
    path = prepared / ("a" * 24 + ".png")
    path.write_bytes(body)
    entry = sample().model_dump()
    entry["prepared_sha256"] = hashlib.sha256(body).hexdigest()
    manifest = {
        "revision": "synthetic-revision",
        "metadata_sha256": "c" * 64,
        "pipeline_version": "voxel51-preparation-v1",
        "samples": [entry],
    }
    raw = (json.dumps(manifest, sort_keys=True) + "\n").encode()
    (root / "manifest.json").write_bytes(raw)
    reference = tmp_path / "reference.json"
    reference.write_text(
        json.dumps({"manifest_sha256": hashlib.sha256(raw).hexdigest(), "settings": {"count": 1}})
    )
    return root, reference


@pytest.mark.asyncio
async def test_runner_scores_real_agent_response_and_publishes_aggregate_only(
    tmp_path: Path,
) -> None:
    root, reference = dataset(tmp_path)
    output = tmp_path / "baseline.json"
    async with GatewayClient(
        gateway_settings(), transport=httpx2.MockTransport(lambda _: model_response())
    ) as gateway:
        await run_baseline(dataset=root, reference_report=reference, output=output, gateway=gateway)
    report = json.loads(output.read_text())
    assert report["tier_metrics"]["A"]["overall"]["f1"] == "1.0000"
    assert report["tier_metrics"]["B"] is None
    assert report["samples"][0]["status"] == "EXTRACTED"
    assert report["samples"][0]["audit_event"] == "extraction.completed"
    assert report["usage"]["input_tokens"] == 1000
    assert output.is_file()
    assert "Synthetic Supplier 001" not in output.read_text()


@pytest.mark.asyncio
async def test_runner_refuses_changed_dataset_before_model_call(tmp_path: Path) -> None:
    root, reference = dataset(tmp_path)
    (root / "prepared" / ("a" * 24 + ".png")).write_bytes(b"changed")
    output = tmp_path / "baseline.json"
    calls: list[httpx2.Request] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        calls.append(request)
        return model_response()

    async with GatewayClient(
        gateway_settings(), transport=httpx2.MockTransport(respond)
    ) as gateway:
        with pytest.raises(ValueError, match="checksum"):
            await run_baseline(
                dataset=root, reference_report=reference, output=output, gateway=gateway
            )
    assert not calls and not output.exists()


def test_settings_use_only_litellm_env_and_extract_model_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LITELLM_API_BASE", "https://gateway.example.test/v1")
    monkeypatch.setenv("LITELLM_MASTER_KEY", "synthetic-secret")
    monkeypatch.setenv("LITELLM_MODEL", "synthetic-general")
    monkeypatch.setenv("LITELLM_EXTRACT_MODEL", "synthetic-vision")
    settings = BaselineSettings(_env_file=None).gateway_settings()
    assert settings.base_url.host == "gateway.example.test"
    assert settings.aliases["extract-vision"].model_name == "synthetic-vision"
    assert settings.aliases["extract-vision"].model_version == "synthetic-vision"
    assert settings.aliases["extract-vision"].response_format == "json_schema"


@pytest.mark.asyncio
async def test_runner_refuses_symlink_outside_dataset(tmp_path: Path) -> None:
    root, reference = dataset(tmp_path)
    outside = tmp_path / "outside.png"
    outside.write_bytes(png_bytes())
    prepared = root / "prepared" / ("a" * 24 + ".png")
    prepared.unlink()
    prepared.symlink_to(outside)
    output = tmp_path / "baseline.json"
    async with GatewayClient(
        gateway_settings(), transport=httpx2.MockTransport(lambda _: model_response())
    ) as gateway:
        with pytest.raises(ValueError, match="escapes the dataset"):
            await run_baseline(
                dataset=root, reference_report=reference, output=output, gateway=gateway
            )
    assert not output.exists()
