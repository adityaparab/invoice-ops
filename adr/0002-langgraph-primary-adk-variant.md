# ADR 0002: LangGraph as primary orchestrator, Google ADK as comparison variant

- **Status:** Accepted
- **Date:** 2026-08-27
- **Deciders:** Architecture (this repo)

## Context

The pipeline is a state machine with durable execution, per-node checkpointing, HITL
pauses, and retries. Candidate frameworks: LangGraph (explicit graph, Postgres
checkpointer, framework-agnostic model access) and Google ADK (agent-first, hierarchical
agents with native tools, strong Google-cloud fit). The target roles value demonstrated
framework judgment over loyalty to any one framework, and the JD stack names "ADK" and
internal libraries.

## Decision

**LangGraph is the primary implementation** of the invoice-processing graph; the same
pipeline is additionally ported to **Google ADK** (Phase 6) as a comparison variant. A
dedicated ADR written at that point compares: checkpointing & durable execution, HITL
support, observability integration, developer ergonomics, and cloud fit — backed by
running the same golden-set eval against both.

## Consequences

- The comparison is evidence-based rather than rhetorical; results land in the ADR and
  the experiment log.
- Business logic (tools, policy, ledger) is shared between both variants, so the ADK
  port measures framework differences, not duplicated-code drift.
- Cost: a second implementation to maintain through Phase 6; after the ADR, the variant
  is frozen (evaluation artifact, not a parallel product).
