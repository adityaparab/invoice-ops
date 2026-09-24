"""Metrics contracts and LiteLLM spend-log sampling without external traffic."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import httpx
import pytest
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, generate_latest

from invoiceops_agent.api.app import create_app
from invoiceops_agent.gateway_client.schemas import TokenUsage
from invoiceops_agent.gateway_client.spend_logs import (
    SpendLogReader,
    SpendLogSampler,
    SpendLogSettings,
)
from invoiceops_agent.gateway_client.telemetry import GatewayEvent, MetricGatewayTelemetry
from invoiceops_agent.obs.metrics import metrics_session, usd_to_nano_usd

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def metric_value(rendered: str, name: str) -> Decimal:
    for line in rendered.splitlines():
        if line.startswith(name + "{") or line.startswith(name + " "):
            return Decimal(line.rsplit(" ", 1)[-1])
    raise AssertionError(f"Missing metric: {name}")


async def test_metrics_endpoint_exposes_request_counts_without_identifiers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_spend_fetch(self: SpendLogSampler) -> None:
        return None

    monkeypatch.setattr(SpendLogSampler, "refresh", no_spend_fetch)
    app = create_app()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            await client.get("/healthz", headers={"X-Trace-ID": "a" * 32})
            response = await client.get("/v1/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith(CONTENT_TYPE_LATEST)
    assert 'invoiceops_api_requests_total{http_request_method="GET"' in response.text
    assert "invoiceops_api_duration_seconds_bucket" in response.text
    assert "a" * 32 not in response.text
    assert "invoiceops_litellm_spend_logs_available 0.0" in response.text
    assert "invoiceops_litellm_spend_last_hour_nusd{window=" not in response.text


async def test_gateway_metrics_record_cost_in_integer_nano_usd() -> None:
    async with metrics_session("unit", prometheus=True) as metrics:
        MetricGatewayTelemetry(metrics).record(
            GatewayEvent(
                run_id=UUID(int=1),
                trace_id="b" * 32,
                prompt_version="prompt-v1",
                scenario="invoice",
                alias="triage-reasoner",
                requested_model="synthetic-model",
                model="synthetic-model",
                model_version="synthetic-model",
                status="succeeded",
                attempts=1,
                latency_ms=1250,
                usage=TokenUsage(input_tokens=20, output_tokens=8, total_tokens=28),
                cost_usd=Decimal("0.0012"),
                budget_alert=True,
            )
        )
        rendered = metrics.render().decode()
    assert "invoiceops_gateway_calls_total" in rendered
    assert "invoiceops_gateway_duration_seconds_sum" in rendered
    assert "invoiceops_gateway_cost_nUSD_total" in rendered
    assert metric_value(rendered, "invoiceops_gateway_cost_nUSD_total") == 1_200_000
    assert "invoiceops_gateway_input_tokens_total" in rendered
    assert "invoiceops_gateway_budget_alerts_total" in rendered
    assert "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb" not in rendered
    assert usd_to_nano_usd(Decimal("0.0000000005")) == 1


async def test_cache_hit_has_separate_metric_without_model_spend() -> None:
    async with metrics_session("unit", prometheus=True) as metrics:
        MetricGatewayTelemetry(metrics).record(
            GatewayEvent(
                run_id=UUID(int=1),
                trace_id="c" * 32,
                prompt_version="faq-v1",
                scenario="public_faq",
                alias="triage-reasoner",
                requested_model="public-model",
                model="public-model",
                model_version="public-model",
                status="succeeded",
                attempts=0,
                latency_ms=2,
                cache_hit=True,
            )
        )
        rendered = metrics.render().decode()
    assert metric_value(rendered, "invoiceops_gateway_cache_hits_total") == 1
    assert "invoiceops_gateway_calls_total" not in rendered
    assert "invoiceops_gateway_cost_nUSD_total" not in rendered


async def test_spend_reader_paginates_exact_decimal_window() -> None:
    seen: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.headers["authorization"] == "Bearer synthetic-key"
        assert request.url.path == "/spend/logs/v2"
        assert request.url.params["start_date"] == "2026-09-24 11:00:00"
        assert request.url.params["end_date"] == "2026-09-24 12:00:00"
        page = int(request.url.params["page"])
        return httpx.Response(
            200,
            json={
                "data": [{"spend": "0.000000001" if page == 1 else "0.000000002"}],
                "page": page,
                "total_pages": 2,
            },
        )

    settings = SpendLogSettings(
        api_base="https://gateway.test/v1", master_key="synthetic-key", _env_file=None
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        total = await SpendLogReader(
            settings, client, clock=lambda: datetime(2026, 9, 24, 12, tzinfo=UTC)
        ).last_hour_usd()
    assert total == Decimal("0.000000003")
    assert len(seen) == 2


async def test_spend_sampler_removes_stale_data_after_proxy_failure() -> None:
    calls = 0
    now = 0.0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 2:
            return httpx.Response(403)
        return httpx.Response(200, json={"data": [{"spend": "0.02"}], "page": 1, "total_pages": 1})

    registry = CollectorRegistry()
    settings = SpendLogSettings(
        api_base="https://gateway.test/v1", master_key="synthetic-key", _env_file=None
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        sampler = SpendLogSampler(SpendLogReader(settings, client), registry, clock=lambda: now)
        await sampler.refresh()
        await sampler.refresh()
        assert calls == 1
        rendered = generate_latest(registry).decode()
        assert metric_value(rendered, "invoiceops_litellm_spend_last_hour_nusd") == 20_000_000
        now = 61.0
        await sampler.refresh()
    rendered = generate_latest(registry).decode()
    assert calls == 2
    assert "invoiceops_litellm_spend_logs_available 0.0" in rendered
    assert "invoiceops_litellm_spend_last_hour_nusd{window=" not in rendered
