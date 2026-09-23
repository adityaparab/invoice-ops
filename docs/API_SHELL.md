# API shell

Run the installed ASGI factory with Python 3.12:

```bash
uv run uvicorn invoiceops_agent.api.app:create_app --factory --host 0.0.0.0 --port 8000
```

The factory does not connect to infrastructure. ASGI lifespan owns the async HTTP client and closes
it on shutdown. PostgreSQL connections are opened for readiness probes and closed immediately after
each check. There are no invoice resources or model calls in this shell.

## Configuration

`ApiSettings` reads the environment when the factory is called. It does not automatically read a
`.env` file; supply environment variables through the shell or the deployment configuration.

| Environment variable | Default | Meaning |
|---|---|---|
| `INVOICEOPS_POSTGRES_DSN` | unset | Secret PostgreSQL URI with `postgresql://` or `postgres://` scheme. |
| `INVOICEOPS_MINIO_URL` | unset | HTTP(S) origin of MinIO, without credentials, path, query, or fragment. |
| `INVOICEOPS_READINESS_TIMEOUT_SECONDS` | `2.0` | Each concurrent readiness probe's time limit, greater than zero and at most 30 seconds. |

Secrets and raw validation inputs are excluded from configuration error diagnostics.

## Health and error contracts

`GET /healthz` returns `200 {"status":"ok"}` without checking any dependencies.

`GET /readyz` checks PostgreSQL with `SELECT 1` and MinIO through `/minio/health/ready`. Successful
probes return `200` with `{"status":"ready","dependencies":{"postgres":"ok","minio":"ok"}}`.
Missing configuration, infrastructure failures, or timeouts return `503 application/problem+json`.
Its `dependencies` extension contains `ok`, `unconfigured`, `unavailable`, or `timeout` per dependency.
The probes run concurrently, are cancellable, and never contact a model. MinIO's readiness endpoint
checks service availability; it does not verify bucket permissions or credentials.

All HTTP errors use RFC 7807 fields `type`, `title`, `status`, `detail`, and `instance`, extended with
`trace_id`. Validation failures return `422`; unexpected failures return sanitized `500` responses.
The response's `X-Trace-ID` matches the problem's `trace_id`. A single supplied lowercase, nonzero,
32-hex `X-Trace-ID` is accepted for correlation; missing or malformed IDs are replaced. IDs are
correlation metadata, not authentication. Request completion, dependency durations in milliseconds,
and failures are logged as key/value fields with the trace ID. Exception messages and connection
strings are excluded from these logs.

Pydantic contracts live in `src/invoiceops_agent/api/schemas/`; matching Zod contracts live in
`frontend/src/schemas/`. The TypeScript files establish the contract for the later frontend scaffold.

## Mutation boundary

All methods other than GET, HEAD, OPTIONS, and TRACE require exactly one `Idempotency-Key`. It must
contain 1–128 ASCII letters, digits, dots, underscores, colons, or hyphens and start with a letter or
digit. Invalid or missing keys return `400` before routing. `get_request_context` is an injectable
FastAPI dependency exposing the validated key and trace ID.

This step validates request metadata only. It has no public mutation endpoints and no durable
idempotency claims, payload hashes, locking, stored responses, or replay. Those guarantees belong to
the transaction implementing each future mutation; accepting a key alone must not be presented as
replay protection.

## Test seam

`create_app(settings, dependency_factory=...)` accepts an async context manager producing typed
`ReadinessChecks`. Offline tests inject probes and fake transports, exercise lifecycle cleanup,
partial outages, timeout cancellation, and HTTP error behavior. Unit tests also run with network
sockets disabled. Production uses the default dependency factory; no import-time network I/O occurs.
