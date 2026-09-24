# Local platform

The Compose stack uses digest-pinned, multiarchitecture images. Python runs as a non-root user;
the application image includes production dependencies, the installed package, and migration assets.

From the repository root:

```bash
cp .env.example .env
docker compose up -d --build --wait
curl --fail http://localhost:8000/healthz
curl --fail http://localhost:8000/readyz
docker compose run --rm seed
```

The default stack starts Postgres and MinIO, then runs the one-shot `migrate` service once Postgres
is healthy. It applies Alembic migrations and provisions the `invoiceops_app` login using
`INVOICEOPS_APP_PASSWORD`. The API and seed service wait for migration success; both also require
healthy Postgres and MinIO. API readiness checks query Postgres and the MinIO readiness endpoint.
The seed entry point inserts the seed-pinned synthetic ERP fixture and treats an exact rerun as a
no-op.

The database owner credentials (`POSTGRES_USER` and `POSTGRES_PASSWORD`) configure Postgres and
the migration connection only. The API receives a separate runtime DSN for `invoiceops_app`, which
can read and append audit entries and perform CRUD on operational tables. It cannot own tables,
alter schema, or truncate tables. Audit update, delete, and truncate operations are also blocked
by database triggers.

The invoice worker uses `LITELLM_API_BASE`, `LITELLM_MASTER_KEY`, and the
model-name variables in `.env.example` directly. No local LiteLLM proxy or
additional route configuration is loaded.

The optional observability stack starts with:

```bash
docker compose --profile observability up -d --wait --wait-timeout 300
```

Langfuse is at `http://127.0.0.1:3000`, Prometheus at `http://127.0.0.1:9090`,
and Grafana at `http://127.0.0.1:3001` with a provisioned Prometheus data source
and platform health dashboard. Langfuse has isolated Postgres, ClickHouse,
Redis, and MinIO dependencies under the same optional profile. See
[observability setup](observability/README.md) for ports, credentials, and checks.

All published ports bind to `127.0.0.1`; `.env.example` lists port overrides and synthetic local
credentials. Change credentials before a shared deployment. Docker build context excludes local
environment files, Git history, tests, and host virtual environments.

Postgres data lives in the `postgres_data` named volume, and MinIO objects live in `minio_data`.
`docker compose down` stops services while retaining both volumes. Start again with `up -d --wait`
to reuse the data. The API keeps no persistent application data in its container.

For native Python development against the running infrastructure:

```bash
export INVOICEOPS_POSTGRES_DSN='postgresql://invoiceops_app:invoiceops-app-local-only-password@localhost:5432/invoiceops'
export INVOICEOPS_MINIO_URL='http://localhost:9000'
uv run uvicorn invoiceops_agent.api.app:create_app --factory --host 127.0.0.1 --port 8002
```

Use the matching application password if `.env` was changed; percent-encode credentials used in
a PostgreSQL DSN. For Compose passwords containing URI-reserved characters, set the corresponding
complete DSN in `.env` with host `postgres`: `INVOICEOPS_MIGRATION_DSN` for the owner (using
`postgresql+psycopg://`) and `INVOICEOPS_POSTGRES_DSN` for `invoiceops_app` (using `postgresql://`).
Keep `INVOICEOPS_APP_PASSWORD` as the raw application password so provisioning and authentication
use the same value. Native commands use `localhost` and the configured `POSTGRES_PORT` instead.

To rotate the application password, update `INVOICEOPS_APP_PASSWORD` in `.env` and any explicit
runtime DSN override, then rerun provisioning and recreate the API with its new credentials:

```bash
docker compose run --rm migrate
docker compose up -d --no-deps --force-recreate --wait api
```

The migration service safely reapplies migrations and updates the managed application login.
Provisioning rejects a conflicting preexisting role instead of taking it over. Changing
`POSTGRES_PASSWORD` in `.env` does not rotate the owner password in an existing Postgres volume;
owner credential management is separate from application password rotation. Logs are available
with `docker compose logs migrate api postgres minio`.

## Upload storage and credentials

The API waits for both owner migrations and the `storage-init` bucket provisioning service.
`INVOICEOPS_SERVICE_TOKEN` authenticates multipart invoice uploads. The separate
`INVOICEOPS_WEBHOOK_SECRET` authenticates the signed synthetic email stub; rotate both outside local
development. The checked-in examples are synthetic local-only configuration.
`INVOICEOPS_RAW_BUCKET` selects the raw-object bucket. See `docs/INGESTION.md` for webhook signing,
nonce, and retry semantics.
Compose passes MinIO credentials only to the API and bucket initializer, separately from database
owner credentials. See [the upload contract](../docs/INGESTION.md) for a curl example, limits,
idempotency, and audited new-key duplicate rejection (`200` with the original IDs).

Run an accepted invoice with the optional one-shot worker using its upload response `run_id`:

```bash
docker compose run --rm invoice-worker invoiceops-invoice-run <run_id>
```

The worker uses the LiteLLM URL, key, and model-name variables directly. See
[workflow operation](../docs/INVOICE_WORKFLOW.md).
Failed runs can be inspected and redriven with `invoiceops-invoice-dlq`; see
[retry and dead-letter operation](../docs/RETRY_AND_DLQ.md).
