"""OpenTelemetry spans stay structured, correlated, and free of payload text."""

from dataclasses import dataclass
from uuid import UUID

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from pydantic import SecretStr

from invoiceops_agent.obs import tracing

pytestmark = pytest.mark.unit


@dataclass(frozen=True)
class Identity:
    run_id: UUID = UUID(int=100)
    invoice_id: UUID = UUID(int=101)
    trace_id: str = "a" * 32


def test_nested_spans_record_only_identifiers_and_error_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(trace, "get_tracer", provider.get_tracer)
    identity = Identity()

    with tracing.operation_span("workflow", "invoice", identity):
        with pytest.raises(RuntimeError, match="sensitive vendor text"):
            with tracing.operation_span("tool", "erp_snapshot", identity):
                raise RuntimeError("sensitive vendor text")

    spans = exporter.get_finished_spans()
    assert [span.name for span in spans] == [
        "invoiceops.tool.erp_snapshot",
        "invoiceops.workflow.invoice",
    ]
    child, parent = spans
    assert child.parent is not None
    assert parent.context is not None
    assert child.parent.span_id == parent.context.span_id
    assert child.attributes is not None
    assert child.attributes["invoiceops.run_id"] == str(identity.run_id)
    assert child.attributes["invoiceops.invoice_id"] == str(identity.invoice_id)
    assert child.attributes["invoiceops.trace_id"] == identity.trace_id
    assert child.attributes["error.type"] == "RuntimeError"
    assert child.status.status_code == StatusCode.ERROR
    assert child.events == ()
    assert "sensitive vendor text" not in str(child)
    provider.shutdown()


def test_export_is_opt_in_and_rejects_malformed_headers() -> None:
    disabled = tracing.TraceSettings(_env_file=None)
    assert tracing.build_tracer_provider(disabled, service_name="invoiceops-test") is None

    malformed = tracing.TraceSettings(
        _env_file=None,
        exporter_otlp_traces_endpoint="http://localhost:3000/api/public/otel/v1/traces",
        exporter_otlp_traces_headers=SecretStr("Authorization"),
    )
    with pytest.raises(ValueError, match="key=value"):
        tracing.build_tracer_provider(malformed, service_name="invoiceops-test")


def test_provider_wires_endpoint_headers_and_service_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class CapturingExporter(InMemorySpanExporter):
        def __init__(self, *, endpoint: str, headers: dict[str, str]) -> None:
            super().__init__()
            captured["endpoint"] = endpoint
            captured["headers"] = headers
            captured["exporter"] = self

    monkeypatch.setattr(tracing, "OTLPSpanExporter", CapturingExporter)
    settings = tracing.TraceSettings(
        _env_file=None,
        exporter_otlp_traces_endpoint="http://localhost:3000/api/public/otel/v1/traces",
        exporter_otlp_traces_headers=SecretStr(
            "Authorization=Basic cGstbGY6c2stbGY=,x-langfuse-ingestion-version=4"
        ),
    )
    provider = tracing.build_tracer_provider(settings, service_name="invoiceops-test")
    assert provider is not None
    monkeypatch.setattr(trace, "get_tracer", provider.get_tracer)
    with tracing.operation_span("tool", "verify_export", Identity()):
        pass
    provider.force_flush()

    assert captured["endpoint"] == "http://localhost:3000/api/public/otel/v1/traces"
    assert captured["headers"] == {
        "Authorization": "Basic cGstbGY6c2stbGY=",
        "x-langfuse-ingestion-version": "4",
    }
    exporter = captured["exporter"]
    assert isinstance(exporter, InMemorySpanExporter)
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].resource.attributes["service.name"] == "invoiceops-test"
    provider.shutdown()
