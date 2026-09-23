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

The optional Compose LiteLLM proxy starts with:

```bash
docker compose --profile gateway up -d --wait
```

It binds to port 4001 to leave port 4000 available for a native developer gateway. Supply the selected
upstream's environment variables first; [gateway configuration](litellm/README.md) describes the
native bridge, Ollama, and production routes. Startup rejects missing variables. The health check
verifies proxy readiness without invoking models. Application gateway traffic is implemented in step 1.5.

All published ports bind to `127.0.0.1`; `.env.example` lists port overrides and synthetic local
credentials. Change credentials before a shared deployment. Docker build context excludes local
environment files, Git history, tests, and host virtual environments.

Postgres data lives in the `postgres_data` named volume, and MinIO objects live in `minio_data`.
`docker compose down` stops services while retaining both volumes. Start again with `up -d --wait`
to reuse the data. The API and proxy keep no persistent application data in their containers.

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

The worker uses the three LiteLLM connection/model variables plus an embedding model name; it
does not use the optional local proxy configuration. See [workflow operation](../docs/INVOICE_WORKFLOW.md).
