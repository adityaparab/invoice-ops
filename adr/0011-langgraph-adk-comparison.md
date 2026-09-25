# ADR 0011: LangGraph and Google ADK invoice workflow comparison

- **Status:** Accepted
- **Date:** 2026-09-25
- **Deciders:** Architecture (this repo)
- **Related decision:** [ADR 0002](0002-langgraph-primary-adk-variant.md)
- **Implementation:** [LangGraph graph](../src/invoiceops_agent/graph/invoice.py), [ADK graph](../src/invoiceops_agent/graph/adk_invoice.py)

## Context and method

Both variants run the `invoice-v1` state machine: Ingest, Extract, Validate, Match3Way,
Policy, Gate, AutoApprove or ExceptionTriage, HumanReview when needed, and Archive or
Reject. They share the typed state, service implementations, deterministic matching and
policy, LiteLLM gateway, and append-only audit ledger. The worker selects the runtime with
`INVOICEOPS_WORKFLOW_ENGINE`; ADK extraction and triage use the Gemini alias named by
`LITELLM_ADK_MODEL` through the same LiteLLM URL and key. The ADK graph uses function
nodes to preserve the existing agent/gateway boundary; it does not use an ADK `LlmAgent`
or a direct Google model endpoint.

Evidence available for this decision: [route and replay tests](../tests/unit/test_invoice_graph.py),
[ADK route and replay tests](../tests/unit/test_adk_invoice.py),
[LangGraph Postgres restart test](../tests/integration/test_invoice_graph.py), and
[ADK Postgres restart test](../tests/integration/test_adk_invoice.py). PR #53 passed the
Python CI job and the golden report gate. The full ADK golden-set measurement belongs to
plan step 6.3 and will be added here after it runs. Until then, this record makes no
comparative quality, latency, or cost claim.

## Comparison

| Dimension | LangGraph primary | Google ADK 2.0 variant | Finding for this system |
| --- | --- | --- | --- |
| Checkpointing | `AsyncPostgresSaver` writes graph state after each transition with `durability="sync"` in the `langgraph` schema. | `DatabaseSessionService` writes node events and session state in an isolated `adk` schema. The runner reloads `invoice_state` for replay. | Both preserve completed and paused runs by `run_id`. LangGraph exposes a graph checkpoint directly; ADK requires session/event interpretation in our adapter. |
| Durable execution | The runner resumes the saved thread; business services reconcile committed ledger evidence if a node's side effect precedes its checkpoint. | The runner resumes an ADK invocation ID from persisted events; the same ledger reconciliation protects repeated node execution. | Postgres restart and human resume are tested for both. A process crash during an unfinished ADK node has not been fault-injected, so its recovery guarantee is not claimed here. |
| Human review | `interrupt` pauses; `Command(resume=...)` supplies a validated `ReviewDecision`. | `RequestInput` pauses with a structured response schema; a matching `adk_request_input` function response supplies the decision. | Both preserve the two-person decision API. ADK needs the stored interrupt ID and invocation ID, making the application adapter longer. |
| Observability | The shared node and tool wrappers emit OpenTelemetry spans with run and trace IDs. Postgres checkpoints can be inspected independently. | The same wrappers emit spans; ADK additionally provides an event stream for routes and pauses. | Existing dashboards retain their business signals. ADK 2.0 pins OpenTelemetry 1.41, which required an isolated Prometheus-reader adapter and dependency compatibility checks. |
| Developer ergonomics | A typed `StateGraph` names nodes and conditional edges; the compiled graph and checkpoint APIs map closely to the invoice state. | A `Workflow` names function nodes and `Event(route=...)` dispatch. Each node adapter serializes and revalidates `InvoiceGraphState` through ADK session state. | LangGraph uses less adaptation for the current typed, deterministic workflow. ADK's structured input event is useful, but the runner must manage function response IDs. |
| Cloud fit | The current container, Postgres, MinIO, and LiteLLM design can run on container infrastructure without a framework-specific cloud service. | ADK documents Cloud Run, GKE, and Google Agent Runtime deployment paths. This repository still uses its existing worker container, Postgres sessions, MinIO, and LiteLLM gateway. | ADK has a clearer Google-managed agent deployment path, but no Google Cloud deployment was tested and this application has no need to replace its current infrastructure. |

## Decision

Retain LangGraph as the production default. Keep the ADK variant as an executable
comparison and evaluation artifact. The deterministic controls, human signoff, gateway,
and audit contract are the product boundaries, independent of orchestration framework.

The planned step 6.3 live evaluation changes both orchestration and the extraction/triage
model route to Gemini. Any metric difference therefore describes the **whole variant**;
it cannot isolate a framework effect from a model effect. Route-parity and restart tests
are the framework-specific evidence available before that measurement.

## Operational consequences and limits

- `INVOICEOPS_WORKFLOW_ENGINE=adk` requires `LITELLM_ADK_MODEL` and the existing
  `LITELLM_EMBED_MODEL`; the worker still uses only the configured LiteLLM base URL and key.
- ADK sessions have their own `adk` PostgreSQL schema and must be backed up and monitored
  alongside LangGraph's `langgraph` schema. A run is resumed with the same engine that
  originally processed it; switching engines for a paused run is not a migration path.
- The ADK 2.0 OpenTelemetry pin affects the shared package. Compatibility is covered by
  Python, integration, and Compose CI, and future dependency upgrades must retest it.
- No comparative live result is asserted until the versioned step 6.3 report is produced.

## Sources

- [Google ADK graph workflows](https://adk.dev/graphs/), [graph human input](https://adk.dev/graphs/human-input/), and [resume behavior](https://adk.dev/runtime/resume/)
- [Google ADK deployment options](https://adk.dev/deploy/) and [Cloud Run guide](https://adk.dev/deploy/cloud-run/)
