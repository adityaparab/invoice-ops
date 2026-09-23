# ADR 0001: Deterministic matcher and policy engine instead of LLM-judged matching

- **Status:** Accepted
- **Date:** 2026-08-27
- **Deciders:** Architecture (this repo)

## Context

The 3-way match (invoice ↔ purchase order ↔ goods receipt) and policy/compliance checks
(spend limits, approval matrix, stale POs) are the control points of an auditable
accounts-payable system. An LLM could perform these comparisons, but the system must
satisfy audit and model-risk expectations (README §8): controls may not depend solely on
a model that is being validated elsewhere in the same pipeline. LLM outputs are also
non-deterministic, which makes replay, regression testing, and per-decision
reconstruction expensive.

## Decision

Matching and policy are implemented as **deterministic, pure code** in
`src/tools/`: same inputs + same versioned config (tolerance bands, rule tables) ⇒ same
outputs, always. The LLM's role is confined to what it is genuinely needed for —
extraction from unstructured documents, exception classification/triage reasoning, and
evidence summarization — behind the boundaries defined in `AGENTS.md`.

## Consequences

- Every match/policy decision is exactly reconstructible from its inputs + config
  version, which the ledger pins per entry (ADR 0004).
- Tolerance bands and policy rules become versioned configuration; changing them is a
  visible, reviewable change (not a prompt tweak).
- Unit/property testing covers the controls fully (hypothesis over boundary behavior).
- Trade-off: purely visual or free-text judgments (e.g. "is this a duplicate with a
  typo" beyond near-dup embeddings) are handled by escalation to humans or the triage
  agent rather than silently guessed.
