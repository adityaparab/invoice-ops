"""Offline checks for optional observability wiring and direct LiteLLM access."""

import json
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from invoiceops_agent.gateway_client.settings import GatewaySettings

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


def test_optional_observability_services_are_pinned_and_isolated() -> None:
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    services = compose["services"]
    observability = {
        "langfuse-postgres",
        "langfuse-clickhouse",
        "langfuse-redis",
        "langfuse-minio",
        "langfuse-web",
        "langfuse-worker",
        "prometheus",
        "grafana",
    }
    for name in observability:
        service = services[name]
        assert service["profiles"] == ["observability"]
        if name == "langfuse-minio":
            assert service["image"] == services["minio"]["image"]
            assert service["pull_policy"] == "never"
        else:
            assert "@sha256:" in service["image"]
    minio_dockerfile = (ROOT / "deploy/minio/Dockerfile").read_text(encoding="utf-8")
    minio_preparation = (ROOT / "scripts/prepare_minio_image.sh").read_text(encoding="utf-8")
    assert "@sha256:" in minio_dockerfile
    assert "expected_sha256=" in minio_preparation
    assert "litellm" not in services
    assert not list((ROOT / "deploy" / "litellm").glob("*.yaml"))
    worker_env = services["invoice-worker"]["environment"]
    assert set(name for name in worker_env if name.startswith("LITELLM_")) == {
        "LITELLM_API_BASE",
        "LITELLM_MASTER_KEY",
        "LITELLM_MODEL",
        "LITELLM_ADK_MODEL",
        "LITELLM_EXTRACT_MODEL",
        "LITELLM_TRIAGE_MODEL",
        "LITELLM_EMBED_MODEL",
        "LITELLM_EXTRACT_PUBLIC_MODEL",
        "LITELLM_EXTRACT_FALLBACK_MODEL",
        "LITELLM_EXTRACT_PUBLIC_FALLBACK_MODEL",
        "LITELLM_TRIAGE_PUBLIC_MODEL",
        "LITELLM_TRIAGE_FALLBACK_MODEL",
        "LITELLM_TRIAGE_PUBLIC_FALLBACK_MODEL",
        "LITELLM_EMBED_PUBLIC_MODEL",
        "LITELLM_EMBED_FALLBACK_MODEL",
        "LITELLM_EMBED_PUBLIC_FALLBACK_MODEL",
    }


def test_grafana_dashboard_uses_its_provisioned_prometheus_source() -> None:
    base = ROOT / "deploy" / "observability"
    prometheus = yaml.safe_load((base / "prometheus.yml").read_text(encoding="utf-8"))
    datasource = yaml.safe_load(
        (base / "grafana/provisioning/datasources/prometheus.yml").read_text(encoding="utf-8")
    )["datasources"][0]
    provider = yaml.safe_load(
        (base / "grafana/provisioning/dashboards/invoiceops.yml").read_text(encoding="utf-8")
    )["providers"][0]
    dashboard = json.loads(
        (base / "grafana/dashboards/platform-health.json").read_text(encoding="utf-8")
    )
    assert datasource["url"] == "http://prometheus:9090"
    assert prometheus["scrape_configs"][0]["static_configs"][0]["targets"] == ["prometheus:9090"]
    assert provider["options"]["path"] == "/var/lib/grafana/dashboards"
    assert dashboard["uid"] == "invoiceops-platform-health"
    assert all(panel["datasource"]["uid"] == datasource["uid"] for panel in dashboard["panels"])


def test_gateway_settings_never_load_a_second_environment_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INVOICEOPS_GATEWAY_BASE_URL", "https://unused.example.test/v1")
    monkeypatch.setenv("INVOICEOPS_GATEWAY_API_KEY", "synthetic-unused-key")
    monkeypatch.setenv("INVOICEOPS_GATEWAY_ALIASES", "{}")
    with pytest.raises(ValidationError):
        GatewaySettings.model_validate({})
