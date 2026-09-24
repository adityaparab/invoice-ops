# ADR 0010: Harden the application gateway with explicit public caching

- **Status:** Accepted
- **Date:** 2026-09-24
- **Deciders:** Project owner and implementation team
- **Extends:** [ADR 0008](0008-direct-litellm-environment.md)

## Context

The plan calls for task and sensitivity routing, semantic caching, fallback, and budget alerts.
The operator supplies a direct LiteLLM URL, key, and model names, and has ruled out any additional
LiteLLM configuration source. Invoice contents and decisions are consequential, so approximate
cache reuse cannot be implicit.

## Decision

The guarded application gateway selects a task-specific model name for a `restricted` request by
default. An explicit `public` tier may select a separate model name. Every additional route is an
optional `LITELLM_*_MODEL` variable in `.env.example`. After bounded infrastructure retries, the
gateway may use the tier's fallback model under the same total deadline. Business and validation
failures do not trigger fallback.

Semantic caching is opt-in only for public, text-only `public_` scenarios. A pgvector table stores
embeddings and validated public responses, with versioned namespacing, dimension isolation, exact numeric anchors,
one-day expiry, and bounded size. Invoice workflows never request it. Cache failure leaves the
normal model path available; cache hits report no completion spend or tokens. A fresh embedding
lookup remains a normal LiteLLM call.

The gateway emits an advisory per-run alert at $0.04 of observed response-header cost. A
Prometheus rule surfaces the event. This does not enforce a global budget or infer missing spend.

## Consequences

The application remains on one LiteLLM endpoint and uses only the existing URL/key plus named
model variables. Operators must choose public and fallback routes with suitable data handling and
capabilities. Approximate cache reuse is safe only for explicitly public, stable-answer tasks; a
future caller must justify opting in. The cache table is operational and mutable, separate from
append-only audit history. Route and cache outcomes remain visible in sanitized telemetry.
