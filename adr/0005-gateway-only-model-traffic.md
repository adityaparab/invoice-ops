# ADR 0005: All model traffic through the LLM Gateway — implemented as a LiteLLM proxy

- **Status:** Superseded in part by [ADR 0008](0008-direct-litellm-environment.md)
- **Date:** 2026-08-27
- **Amended:** 2026-08-27 — LiteLLM named as the gateway implementation
- **Deciders:** Architecture (this repo)

## Context

Model calls need routing (task class → model), guardrails (PII redaction, injection
heuristics, schema validation), budgets, caching, cost telemetry, and provider
fallback — uniformly, not per-call-site. The portfolio's Project 3 is a full LLM
Gateway; this project must not depend on it existing yet, and dev iteration should run
on free local models while prod/eval use hosted OpenAI models.

## Decision

1. Application code never uses provider SDKs directly. All model traffic flows through
   **one thin wrapper** (`src/invoiceops_agent/gateway_client/`) over a single OpenAI-compliant endpoint.
2. That endpoint is a **LiteLLM proxy**: model routing, fallback chains, budgets, and
   spend logging are configuration (`deploy/litellm/config.yaml`).
3. Applications address models **only by virtual aliases** (`extract-vision`,
   `triage-reasoner`, `eval-judge`, `embed`). Aliases map to local Ollama-backed models
   in dev and OpenAI models in prod/eval. **Switching environments is a config change,
   never a code change.**
4. The wrapper adds what LiteLLM does not: response schema validation, typed
   retry-or-escalate, and ledger/trace hooks.

## Consequences

- Project 3's gateway can replace the proxy later without touching call sites.
- Per-alias cost/latency telemetry comes free from LiteLLM spend logs.
- One more service in Compose; model *names* remain configurable, documented in the
  config file header.
- Direct provider SDK calls appearing anywhere outside `src/invoiceops_agent/gateway_client/` are bugs.
