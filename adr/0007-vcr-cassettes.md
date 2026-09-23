# ADR 0007: VCR-style recorded LLM responses for deterministic tests

- **Status:** Accepted
- **Date:** 2026-08-27
- **Deciders:** Architecture (this repo)

## Context

Integration tests of agent nodes need realistic LLM responses, but live calls are
non-deterministic, slow, network-dependent, and cost money — fatal for CI reliability
and for the "flaky test" discipline in `AGENTS.md`. Eval runs (Phase 5) are the only
place real models should execute, and they run deliberately.

## Decision

- All LLM interactions in unit/integration tests replay **committed cassettes** —
  recorded once through the gateway wrapper (per alias + scenario) and replayed
  offline.
- Cassette identity: alias + scenario name + prompt version. A new prompt version
  requires a new scenario/cassette; existing cassettes are never edited to make a test
  pass.
- The gateway client exposes the record/replay seam so agents are unaware of the mode.
- A `--recorded` mode of the eval runner uses the same machinery for CI smoke runs.

## Consequences

- Test suites are deterministic, offline, and fast; prompt regressions surface as
  cassette mismatches rather than flaky behavior.
- Cassettes are committed artifacts and grow with scenarios (bounded by catalog size).
- Drift risk between cassettes and live behavior is checked by scheduled real-model
  eval runs (Phase 5 CI strategy).
