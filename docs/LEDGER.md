# Transactional ledger writer and reader

Step 1.4 is brought forward as a prerequisite for audited ingestion. It provides library operations
under `invoiceops_agent.ledger`; it adds no HTTP mutation or read endpoints. Database append-only
grants and triggers come from step 0.7 and remain the final enforcement boundary.

## Append with business data

`LedgerWriter.append(connection, command, trace_id=...)` requires an already-active caller-owned
transaction using PostgreSQL’s default READ COMMITTED isolation and a psycopg-compatible async
connection returning dictionary rows. It never opens,
commits, rolls back, or closes a transaction or connection. A returned `LedgerEvent` is staged, not
yet durable. The caller commits business rows, ledger events, and idempotency responses together.
The caller also configures bounded database connection, statement, and lock timeouts.

The writer locks the associated `runs` row before allocating the next sequence. This serializes
cooperating writers on the run without requiring forbidden update privileges on `ledger`. It checks
that the invoice and graph version agree with the run, and that an optional superseded event belongs
to the same run and invoice. Corrections create new events; there are no update or delete methods.

A clock and UUID factory are injectable. Naive timestamps are rejected; aware timestamps normalize
to UTC. Payloads must be JSON values with finite numbers. Monetary values in payloads should use
Decimal-safe strings such as `"12.34"`, never floating-point conversions.

A uniqueness or integrity failure raises `LedgerConflict`; infrastructure and malformed-storage
failures raise sanitized `LedgerStorageError`. Business identity mismatches and missing runs have
separate typed errors. Helpers never retry. After any database error the caller must roll back its
transaction before retrying the entire idempotent business operation when appropriate.

## Provenance configuration

`LedgerSettings` requires all four nonblank pins from constructor arguments or environment:

- `INVOICEOPS_LEDGER_GRAPH_VERSION`
- `INVOICEOPS_LEDGER_MODEL_VERSION`
- `INVOICEOPS_LEDGER_PROMPT_VERSION`
- `INVOICEOPS_LEDGER_POLICY_VERSION`

There are no default versions. For an ingestion event with no model, prompt, or policy evaluation,
the caller explicitly configures a versioned sentinel, such as `not-applicable@v1`. A
`VersionOverrides` on an append command can replace relevant pins for an extraction agent or policy
step while retaining the other configured versions. The resolved graph pin must match the run.

An initial successful upload should stage a SYSTEM `ingest.accepted` event in the same transaction
as its invoice, queued run, and durable replay response. Replaying that response must not append a
second acceptance event. This library does not itself implement upload idempotency or content dedupe.

## Bounded history reads

`LedgerReader.for_run` reads one run in ascending sequence order.
`LedgerReader.for_invoice` reads across all of an invoice's runs in ascending `(created_at, id)`
order. Each page uses one query fetching `limit + 1` rows; page sizes are 1–200, default 50. There is
no offset scanning or per-entry lookup. A continuation cursor belongs to its original scope and is
returned only when another row is known to exist. A missing scope returns an empty page.

Rows are immutable, so existing rows do not move during pagination. Independent page requests use
the caller's transaction isolation and do not promise a historical snapshot: newly inserted events
can appear in later pages, and an event inserted behind an invoice cursor (for example with a
backdated clock or a lower UUID at the same timestamp) requires a refresh to discover. Callers needing
a fixed database snapshot must use an appropriate repeatable-read transaction for the whole read.

Pydantic append/event/page contracts live in `ledger/schemas.py`; their Zod twins are in
`frontend/src/schemas/ledger.ts`. These contracts prepare later provenance resources without adding
those endpoints early.

## Observability and tests

Append logs say `ledger_append_staged`, avoiding a claim that the caller has committed. Read and
write logs include run/invoice context where available, the request trace, row counts or sequence,
and duration in milliseconds. Payloads, actor names, connection strings, and exception messages are
excluded. Caller-owned transactions and connections make failure and rollback seams testable.

Offline tests use fake async query results. Real PostgreSQL tests apply the full migration chain and
exercise runtime-role privileges, rollback with business rows, concurrent sequence allocation,
supersession, version pins, and run/invoice pagination. No live model is required.
