# Durable hello graph

Step 0.8 implements `START → hello_start → hello_finish → END` using LangGraph. Both nodes are
explicit hello stubs: they do not extract, match, approve, call a model, or emit a business ledger
event. The production invoice workflow remains a later plan step.

## Run the demo

Set `INVOICEOPS_CHECKPOINT_DSN` to a PostgreSQL URI for the checkpoint database, then run:

```bash
uv run invoiceops-graph-demo \
  --run-id 00000000-0000-4000-8000-000000000001 \
  --invoice-id 00000000-0000-4000-8000-000000000002
```

Both IDs are optional; omitted IDs are generated UUIDs and included in the structured result log.
Pass both original IDs to replay or resume a run. The CLI exits with status 1 on configuration or
graph failures and excludes exception messages and connection credentials from its logs.

`GraphSettings` reads the environment when instantiated, without automatically reading a `.env`
file. `INVOICEOPS_GRAPH_TIMEOUT_SECONDS` defaults to 30 and must be greater than zero and at most
300. Database connection establishment and individual statements have five-second limits.

## Compose demo

The `graph-demo` service uses the API image and the `tools` profile. It waits only for healthy
PostgreSQL, because LangGraph manages its own schema independently of application migrations.
Build the image and execute the same pair of IDs twice:

```bash
docker compose build graph-demo
docker compose run --rm graph-demo invoiceops-graph-demo \
  --run-id 00000000-0000-4000-8000-000000000001 \
  --invoice-id 00000000-0000-4000-8000-000000000002
docker compose run --rm graph-demo invoiceops-graph-demo \
  --run-id 00000000-0000-4000-8000-000000000001 \
  --invoice-id 00000000-0000-4000-8000-000000000002
```

The second execution logs `graph_replayed` and returns the persisted state. CI repeats this smoke
after starting the default Compose stack. Neither execution performs a business decision.

Compose defaults `INVOICEOPS_CHECKPOINT_DSN` to the synthetic local PostgreSQL owner credentials.
Override it with a percent-encoded owner-capable DSN if needed. This explicit demo connection creates
the isolated `langgraph` schema and saver tables; it is separate from the application's restricted
runtime database role. A migration-service dependency is unnecessary for these managed tables.

## Persistence and replay

An async context manager owns one psycopg connection, the Postgres checkpointer, and the compiled
graph. It creates LangGraph's managed tables in the dedicated `langgraph` schema, using a restricted
`search_path`. The separate application table `public.checkpoints` is untouched. A database advisory
lock serializes saver setup, including its versioned library migrations.

The saver uses MessagePack with an explicit allowlist containing the pinned `GraphState` model and
LangGraph's built-in safe types. Pickle fallback is disabled. Pydantic revalidates loaded graph state;
its contract, mirrored by `frontend/src/schemas/graph.ts`, pins `hello-v1`, the `hello-stubs` workflow,
identities, trace ID, and consistent progress.

Runs use their `run_id` as LangGraph's durable thread ID. Synchronous durability writes each
checkpoint before advancing to the next node. An interrupted run resumes pending work by invoking
with `None`; it does not resend initial state. A completed run returns its persisted state without
re-executing nodes. Reusing a run ID for a different invoice raises `RunConflict`.

A local async lock serializes calls sharing one runtime connection. A PostgreSQL session advisory
lock prevents another process from executing that run concurrently; contention returns the typed
`RunInProgress` error immediately. Locks release after the run's checkpoint writes complete and on
failure, and the context manager closes its connection on shutdown. Cancellation during lock
acquisition or release discards the connection when lock ownership is uncertain; open a new runtime
before retrying. This is a small single-connection hello scaffold, not the future worker concurrency
architecture.

Node completion, run results, duration in milliseconds, and sanitized failures use structured
key/value logs with `run_id` and `trace_id`. Completion means only that both hello stubs executed;
it does not imply an invoice approval or audit-ledger decision.

## Verification

Offline tests inject in-memory checkpoints and stub-node failures. They verify checkpoints after
each node, completed replay, invoice conflicts, cancellation, recovery, serializer restrictions, and
CLI behavior. Testcontainer integration tests reopen actual PostgreSQL connections to prove durable
restart/resume, preserve a sentinel `public.checkpoints` table, and test competing runtimes.
