# Invoice queue and detail reads

`GET /v1/invoices` accepts bearer tokens from `INVOICEOPS_ANALYST_TOKEN` or
`INVOICEOPS_MANAGER_TOKEN`. `GET /v1/invoices/{id}` also accepts
`INVOICEOPS_AUDITOR_TOKEN`. The upload service token cannot read invoices.
Role tokens must be distinct, at least 16 printable ASCII characters, and
configured before reads are enabled. They are supplied to the API through its
environment, including the Compose `api` service.

The queue accepts `status`, `run_status`, `source`, `exception_only`,
`min_priority` (0–3), `limit` (1–100, default 50), and an opaque `cursor`.
Results sort by invoice creation time and ID, newest first. Follow
`next_cursor` to fetch the next page using the same filters. Invalid cursors
return HTTP 400; invalid filters return HTTP 422. Missing, invalid, or duplicate
authorization headers return 401. An auditor attempting to list the queue
receives 403. All error bodies are `application/problem+json` and include a
request trace ID.

Each queue row uses the latest run and its latest extraction and exception.
Detail returns the same summary, the full latest exception, and committed
evidence keyed by workflow event type. Ledger payloads remain the source of
truth for extraction and decisions. Review entry writes the operational
exception, `NEEDS_REVIEW` invoice status, and `triage.prepared` ledger event in
one transaction. The versioned `exception-queue@v1` projection gives near
duplicates, bank changes, and policy blocks priority 3 with a 4-hour SLA;
other findings or extraction escalation get priority 2 with 24 hours; the
remaining review cases get priority 1 with 72 hours. The clock is injected so
the deadlines and audit evidence are reproducible in tests.

The endpoints are read only. The separate [decision endpoint](EXCEPTION_DECISIONS.md)
records the analyst proposal and manager signoff.
