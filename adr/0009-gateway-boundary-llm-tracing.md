# ADR 0009: Trace model calls at the application gateway boundary

- **Status:** Accepted
- **Date:** 2026-09-24
- **Deciders:** Project owner
- **Supersedes:** Phase 4.3's planned LiteLLM proxy callback configuration

## Context

The operator supplies a direct LiteLLM URL, key, and model names and prohibits
additional LiteLLM configuration (ADR 0008). LiteLLM's Langfuse proxy callback
requires configuration in the LiteLLM deployment, which this repository does
not own. The application already routes every model and embedding request
through `GatewayClient`.

## Decision

`GatewayClient` opens one OpenTelemetry observation span per logical chat or
embedding call, including its local retries. Its existing sanitized outcome
callback fills the span with the requested and returned model, version pins,
attempt count, latency, token usage, and a cost only when LiteLLM reports one.
Failures carry a typed code. No prompt, invoice body, model output, embedding
vector, credential, or provider error message enters the span. The existing
opt-in OTLP/HTTP exporter sends these spans to Langfuse using its documented
observation and GenAI attribute mapping.

No LiteLLM proxy callback, provider route, or second LiteLLM configuration is
introduced. `LITELLM_API_BASE`, `LITELLM_MASTER_KEY`, and named
`LITELLM_*_MODEL` variables remain the only LiteLLM inputs.

## Consequences

All model calls made by this application are traced when OTLP export is
configured, including calls that fail before transport. Calls made by other
clients of the operator's LiteLLM deployment are outside these application
traces. Prompt and response payloads are deliberately absent; audit evidence
and version pins remain in the ledger. A missing response cost stays unknown
rather than becoming zero. Spend-log reconciliation follows in step 4.4.
