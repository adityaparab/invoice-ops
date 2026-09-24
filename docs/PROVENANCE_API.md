# Trace and invoice provenance reads

Step 3.12 adds two auditor-only, read-only endpoints. They require the auditor
persona token; analyst, manager, and upload service credentials cannot read
full provenance. Errors use the API's RFC 7807 response with a request trace ID.

`GET /v1/runs/{run_id}/trace` returns the run's identity, persisted trace ID,
graph version, lifecycle status and timestamps, and committed ledger event
metadata in sequence order. Each event includes its actor, node, correction
link, timestamp, and four version pins. The trace intentionally omits event
payloads; the existing `/v1/runs/{run_id}/ledger` endpoint supplies those to
auditors. Use `limit` (1–200, default 50) and the returned `next_cursor.sequence`
as `after_sequence` for the next page.

`GET /v1/invoices/{invoice_id}/provenance` returns the invoice's identity,
status, source, creation time, and full immutable ledger events across all its
runs. It uses `(created_at, id)` ordering to make same-timestamp ties stable.
Pass both `next_cursor.created_at` as `after_created_at` and `next_cursor.id`
as `after_event_id` to continue; sending only one is a 400 error. Its `limit`
has the same 1–200 range. A missing run or invoice returns 404.

Both endpoints read their header and ledger page in one repeatable-read,
read-only transaction through the restricted runtime role. A later HTTP page
has a new snapshot and can include newly appended events; callers wanting an
export of the latest history should fetch all pages afresh. No endpoint writes
to the append-only ledger or decisions tables.

The Pydantic contracts are mirrored by `frontend/src/schemas/provenance.ts`;
the frontend API helpers parse each response with those Zod schemas. Offline
authorization/cursor tests and real restricted-role Postgres tests cover both
resources without model traffic.
