# ADR 0004: Append-only audit ledger with point-in-time version pinning

- **Status:** Accepted
- **Date:** 2026-08-27
- **Deciders:** Architecture (this repo)

## Context

Every automated and human decision must be reconstructible after the fact (README goal
G3; OCC-successor documentation expectations and EU AI Act Art. 12 record-keeping, per
README §8). Mutable audit records would undermine provenance: a decision's explanation
must be the one that existed *at decision time*, including which model, prompt, and
policy produced it.

## Decision

The `ledger` (and `decisions`) tables are **append-only**:

- Enforcement is technical, not conventional: DB triggers reject UPDATE/DELETE, and the
  application DB role lacks those grants (issue #7). No repository code path exists to
  modify entries.
- Every entry pins `actor_type` ∈ {SYSTEM, AGENT, HUMAN, POLICY} plus `model_version`,
  `prompt_version`, `policy_version`, and `graph_version`.
- Correcting a wrong entry happens by appending a superseding entry that references it.

## Consequences

- Provenance endpoints (#35) can reconstruct any decision from the ledger alone.
- Storage grows monotonically (acceptable at demo scale; partitioning is the documented
  growth path).
- Schema changes to ledger-referenced data become data-lineage events requiring review.
