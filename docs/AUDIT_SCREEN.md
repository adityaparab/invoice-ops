# Auditor run history

`GET /v1/runs/{run_id}/ledger` is an auditor-only read of immutable run
history. It returns the run and invoice IDs, trace ID, run status, actor identity,
node, payload, correction link, and all four version pins for each event. The
query accepts `limit` (1–200, default 50) and `after_sequence` (positive
integer), and returns a run-bound next cursor. Missing runs return 404;
unauthorized reads use RFC 7807 errors.

The API delegates keyset pagination to the ledger reader under the restricted
runtime database role. Each page reads the run and its events from one
repeatable-read, read-only snapshot. A later page can see events appended after
the earlier page; the ledger sequence keeps pagination forward-only.

Priya's Audit & Provenance screen shows a Mantine timeline and a ledger table
with actor and graph/model/prompt/policy versions. The payload and correction
link expand per event. **Export full run provenance** fetches every page anew
and downloads a JSON document with the run identity, trace ID, status, export
time, and all immutable events. A repeating or cross-run cursor aborts export.
The [trace and invoice-wide provenance endpoints](PROVENANCE_API.md) provide
metadata-only run chronology and cross-run ledger history to auditors.
