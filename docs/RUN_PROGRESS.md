# Agent Run progress

`GET /v1/runs/{run_id}/progress` returns a bounded, read-only projection of one
operational run and its committed audit events. Analyst, manager, auditor, and
service tokens can read it. Other credentials receive an RFC 7807 problem.

The response has the run status and all eleven invoice-v1 node names. A node
has an `observed_at` timestamp and event type only after its mapped audit event
commits. The state panel exposes an explicit subset of that event's output;
document storage references, hashes, bank details, model prompts, and arbitrary
ledger payloads are excluded. The extraction subset includes only vendor name,
invoice number, PO number, currency, and total amount with confidence.

The UI polls every two seconds while a run is queued, running, or paused and
stops after a terminal status. `active_node` is inferred from the run status and
latest observed node, so an in-flight node can be shown before its output is
recorded. The `progress_source: audit-ledger` field makes that distinction
explicit. Full immutable history and export remain the Audit/Provenance work.
