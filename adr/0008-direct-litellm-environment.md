# ADR 0008: Use the configured LiteLLM endpoint directly

- **Status:** Accepted
- **Date:** 2026-09-24
- **Deciders:** Project owner
- **Supersedes:** ADR 0005's local proxy configuration and Compose proxy service

## Context

The operator supplies a LiteLLM base URL, API key, and model name through the
existing `LITELLM_*` environment variables. Maintaining another LiteLLM YAML
route map and a local proxy introduced a second configuration path that could
disagree with those values.

## Decision

The application and evaluation harness call that LiteLLM endpoint through the
single guarded `gateway_client` wrapper. The runtime reads
`LITELLM_API_BASE`, `LITELLM_MASTER_KEY`, and `LITELLM_MODEL`; task-specific
models use only named `LITELLM_*_MODEL` variables documented in
`.env.example`. Internal aliases identify task policies in code; each alias
sends the configured model name to LiteLLM. `GatewaySettings` is an explicit
value object and does not load a separate environment prefix.

The unused Compose LiteLLM proxy, YAML route maps, and selected-file preflight
are removed. The optional observability stack does not route model traffic.

## Consequences

Operators configure provider routing and fallback at the LiteLLM deployment
named by `LITELLM_API_BASE`. A model-name variable may point to a mutable
route; the ledger keeps the configured name and the gateway-reported model,
which should be compared with deployment records for revision-level provenance.
New task classes require an explicit model-name variable in `.env.example`.
The gateway-only call boundary, guards, typed failures, and audit pins from
ADR 0005 remain in force.
