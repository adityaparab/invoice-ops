# ADR 0003: Composite confidence gate with abstention

- **Status:** Accepted
- **Date:** 2026-08-27
- **Deciders:** Architecture (this repo)

## Context

Straight-through processing requires knowing *when the system should not decide*.
Extraction field confidence alone ignores downstream signals: a confidently-read
invoice number on a mispriced line, or a policy finding, must also drive routing. EU AI
Act-style human-oversight expectations (README §8) require escalation for consequential
decisions rather than silent guessing, and tuning a single threshold needs to be an
eval-driven, documented activity.

## Decision

A deterministic **composite gate** decides AUTO vs EXCEPTION:

`confidence = w1·min(field_conf) + w2·(1 − normalized_match_delta) + w3·policy_severity_term`

- Weights `w1..w3` and threshold `τ` live in **versioned config**, not code.
- `confidence < τ` **must** route to ExceptionTriage (abstention). Routing AUTO below τ
  is treated as a correctness bug, and the boundary semantics (conf == τ ⇒ AUTO) are
  unit-tested.
- τ is tuned via the eval harness τ-sweep (ROC-style curve), with each change recorded
  in the experiment log; the recall/false-escalation pair is reviewed together to
  prevent over-escalation drift.

## Consequences

- Escalation behavior is reproducible and auditable; every gate decision records its
  inputs in state and the ledger.
- Tuning τ trades STP rate against false-escalation rate explicitly and measurably.
- The formula is intentionally simple and inspectable; richer calibration (e.g.
  learned calibration) remains future work and would need its own ADR.
