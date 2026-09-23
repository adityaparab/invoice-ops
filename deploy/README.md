# Local platform

The Compose stack uses digest-pinned, multiarchitecture images. Python runs as a non-root user;
only production dependencies and the installed package enter the application image.

From the repository root:

```bash
cp .env.example .env
docker compose up -d --build --wait
curl --fail http://localhost:8000/healthz
curl --fail http://localhost:8000/readyz
docker compose run --rm seed
```

The default stack starts the API, Postgres with pgvector available, and MinIO. Readiness checks query
Postgres and the MinIO readiness endpoint. Database schema migrations arrive in step 0.6. The seed
entry point currently logs `seed.placeholder` and exits without changing data; reproducible ERP
seeding is step 2.1.

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
export INVOICEOPS_POSTGRES_DSN='postgresql://invoiceops:invoiceops-local-only@localhost:5432/invoiceops'
export INVOICEOPS_MINIO_URL='http://localhost:9000'
uv run uvicorn invoiceops_agent.api.app:create_app --factory --host 127.0.0.1 --port 8002
```

Use the matching credentials if `.env` was changed; URL-encode credentials used in a PostgreSQL
DSN. For Compose passwords containing URI-reserved characters, supply a complete percent-encoded
`INVOICEOPS_POSTGRES_DSN` in `.env` with host `postgres`. Logs are available with
`docker compose logs api postgres minio`.
