# Initial database schema

Revision `0001_initial_schema` implements the twelve tables in Architecture §6. The schema uses
UUID primary keys, except the durable external keys in `ingestion_requests` and `webhook_nonces`.
Application tables and Alembic's version table live in the `public` schema explicitly.
PostgreSQL generates UUIDs when the caller does not supply one. All timestamps use `timestamptz`;
the migration connection runs in UTC. Calendar dates (`issued_on`, `due_on`) use `date`.

## Migration commands

Set `INVOICEOPS_MIGRATION_DSN` to the separate owner connection, using the
`postgresql+psycopg://user:password@host:port/database` URL format. The CLI reads it through
`MigrationSettings`; it never falls back to an application-role DSN and does not read `.env`
implicitly. Passwords containing URL delimiters must be percent-encoded. The owner must be able
to install the `vector` extension, or an administrator must install it beforehand.

```sh
uv run alembic upgrade head
uv run alembic current
uv run alembic downgrade base
```

Upgrade and downgrade execute transactionally. Connection, lock, and statement waits are bounded.
Downgrade to `base` deletes all application tables and their data, leaving Alembic's empty version
table. It retains the `vector` extension because the extension can predate InvoiceOps or serve
another schema. A subsequent upgrade recreates the application tables and indexes.

Revision `0001_initial_schema` creates the audit structures and version constraints. Revision
`0002_append_only_audit` adds the enforcement and runtime-role boundaries below.

## Audit enforcement and runtime login

Both `ledger` and `decisions` have `BEFORE UPDATE OR DELETE OR TRUNCATE` statement triggers. They
reject mutation even when no rows match, and reject truncation through `CASCADE` from another table.
The triggers use `ENABLE ALWAYS`, so replication-mode sessions still receive SQLSTATE `55000`.
Normal table-owner writes are subject to this rule; a database administrator remains able to alter
the schema deliberately. Corrections append a new row with `supersedes_id`.

The migration creates `invoiceops_app` as a nonadministrative `NOLOGIN` role and marks it as managed
for this database. It grants database `CONNECT`, schema `USAGE`, audit-table `SELECT`/`INSERT`, and
explicit `SELECT`/`INSERT`/`UPDATE`/`DELETE` on the ten operational tables. It grants neither access
to `alembic_version`, object ownership, persistent schema creation, `TRUNCATE`, nor grant options.
Future tables require explicit grants in their migration; no blanket default privileges exist.

An existing role must have the matching database marker, no administrative attributes, no object
ownership, no inherited runtime memberships or other identities using it, and no privileges in
other databases or on shared administrative objects. A migration owner's admin-only membership
without `INHERIT` or `SET` access is permitted. Unsafe or unrelated roles cause failure; their
credentials and unrelated privileges are not changed. Excess privileges inherited from `PUBLIC`
also cause failure instead of silently weakening the runtime boundary.

Set `INVOICEOPS_APP_PASSWORD` separately, then bootstrap from the repository root:

```sh
uv run python -m invoiceops_agent.db.migrate
```

This owner-only command applies `alembic upgrade head`, verifies the runtime role's restrictions,
then enables its login using a client-generated SCRAM-SHA-256 verifier. The plaintext application
password is read from an environment-backed `SecretStr`, never placed in SQL or logs, and never
stored in a migration. A real runtime login verifies the supplied password. Connection, statement,
and role-provisioning lock waits are bounded; events report role, outcome, and duration without DSNs
or credential material. The Compose `migrate` service runs this command before API startup.

Downgrading revision `0002` removes its triggers, function, and explicit grants. It preserves the
cluster-wide role, login state, and password; it never executes `DROP ROLE` or `DROP OWNED`.
The downgraded schema is no longer immutable for its owner, and the application loses table access.
Reupgrade reapplies the restricted grants and triggers without rotating the retained password;
running the bootstrap command additionally reprovisions the configured password.

## Operational contracts

| Table | Key fields and constraints |
|---|---|
| `vendors` | Unique nonblank `external_id`, nonblank `name`; optional `tax_id` and synthetic `bank_account_iban`. |
| `purchase_orders` | Unique `po_number`, required vendor reference, uppercase three-letter currency, `issued_on`, total, JSONB line array. |
| `goods_receipts` | Unique `receipt_number`, required PO reference, `received_at`, JSONB line array. |
| `invoices` | Unique lowercase 64-character SHA-256 `content_hash`, nonblank immutable `raw_ref`, media type, source and status. Extracted fields and matched vendor/PO references remain nullable until available. |
| `invoice_lines` | Positive line number unique within the invoice; description, quantity, unit price, tax rate, tax amount, and line total. |
| `runs` | Required invoice, nonblank graph version and trace identifier; nullable start/completion times and optional JSONB error object. |
| `checkpoints` | Positive sequence unique within the run, nonblank node and graph version, JSONB state object. This is the application projection; LangGraph saver tables belong in the separate `langgraph` schema. |
| `ingestion_requests` | Nonblank idempotency key up to 128 characters, request hash, invoice/run identity, original successful HTTP status (`200` or `201`) and JSONB response object. |
| `webhook_nonces` | Nonblank nonce up to 128 characters and authenticated `signed_at` timestamp. The timestamp index supports eventual expiry cleanup. |
| `ledger` | Positive sequence unique within the run, event type, actor identity, JSONB payload, all four version pins, and optional `supersedes_id`. |
| `exceptions` | Invoice/run identity, extensible exception type, priority `0`–`3` (zero is highest), SLA deadline, optional assignee, JSONB evidence and recommendation objects. |
| `decisions` | Required exception and its invoice/run identity; unique idempotency key, human actor identity, action, nonblank rationale/reason, all four version pins, and optional `supersedes_id`. |

Foreign keys use the default restrictive deletion behavior, including all audit references. Composite
foreign keys prevent an event, exception, decision, or replay response from naming a run belonging
to a different invoice. Decisions also reference the matching exception/run/invoice tuple.

Both audit tables require non-null, nonblank `graph_version`, `model_version`, `prompt_version`, and
`policy_version`. Callers explicitly supply a versioned sentinel when a component is inapplicable;
there is no automatic default that could hide missing provenance. Ledger actors are `SYSTEM`,
`AGENT`, `HUMAN`, or `POLICY`; decisions require `HUMAN`.

## Status values

| Resource | Allowed states |
|---|---|
| Vendor | `ACTIVE`, `INACTIVE` |
| Purchase order | `OPEN`, `PARTIALLY_RECEIVED`, `CLOSED`, `CANCELLED` |
| Invoice | `RECEIVED`, `QUEUED`, `PROCESSING`, `NEEDS_REVIEW`, `APPROVED`, `REJECTED`, `RETURNED`, `ARCHIVED`, `FAILED` |
| Run | `QUEUED`, `RUNNING`, `PAUSED`, `COMPLETED`, `FAILED`, `CANCELLED` |
| Exception | `OPEN`, `IN_REVIEW`, `RESOLVED`, `ESCALATED` |
| Human decision action | `APPROVE`, `RETURN`, `ESCALATE`, `REJECT` |

Invoice source is `UPLOAD` or `EMAIL`; supported content types are `application/pdf`, `image/png`,
and `image/jpeg`. These checks constrain vocabulary, not workflow transitions. Later orchestration
and repositories validate legal transitions.

## Precision and indexes

Money uses `numeric(18,2)`, quantity uses `numeric(18,4)`, and tax rate/confidence uses
`numeric(9,6)`. PostgreSQL rejects out-of-range values; checks reject numeric NaN. Decimal values
must be passed from Python without conversion to float. Extracted amounts are not constrained to
agree arithmetically: inconsistent source documents must remain representable for validation and
human review. Confidence is constrained to `[0,1]`.

The nullable `invoices.embedding` is `vector(384)` and uses an HNSW `vector_cosine_ops` index.
Invoice status/creation, vendor/PO relationships, PO vendor/status, receipt PO/time, run invoice/start,
exception status/SLA, and audit invoice/creation/UUID indexes support expected access paths. The
run/sequence unique indexes support ordered checkpoints and ledger reads. Ledger and decision
supersession links have indexes for correction-history queries.
