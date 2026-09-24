# Optional local observability stack

Run from the repository root:

```sh
docker compose --profile observability config --quiet
docker compose --profile observability up -d --wait --wait-timeout 300
```

The profile adds Langfuse web and worker, isolated Langfuse Postgres,
ClickHouse, Redis, and MinIO, plus Prometheus and Grafana. It is excluded from
the default invoice-processing stack. The services follow the [Langfuse v4
Compose topology](https://langfuse.com/self-hosting/deployment/docker-compose)
and [official health endpoints](https://langfuse.com/self-hosting/configuration/health-readiness-endpoints).
All published ports bind to loopback.

| Service | Local URL | Check |
| --- | --- | --- |
| Langfuse | `http://127.0.0.1:3000` | `/api/public/ready` |
| Prometheus | `http://127.0.0.1:9090` | `/-/ready` |
| Grafana | `http://127.0.0.1:3001` | `/api/health` |
| Langfuse MinIO API | `http://127.0.0.1:9091` | `/minio/health/ready` |

The matching `*_PORT` variables in `.env.example` override host ports. The
checked-in Langfuse and Grafana credentials are synthetic local defaults;
replace them before use on a shared host. Langfuse's first web user is created
through its setup UI. Grafana uses `GRAFANA_ADMIN_USER` and
`GRAFANA_ADMIN_PASSWORD`.

Grafana provisions a Prometheus data source and the **InvoiceOps Platform
Health** dashboard from files in this directory. This first dashboard shows
live Prometheus scrape health and duration. The API metrics endpoint and
invoice cost/latency panels are plan step 4.4, so no invoice metric is
fabricated here. Prometheus currently scrapes itself; step 4.4 adds the API
target when `/v1/metrics` exists.

To export workflow spans, create a Langfuse project and API keys. Set
`OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` to
`http://langfuse-web:3000/api/public/otel/v1/traces` for Compose processes.
Set `OTEL_EXPORTER_OTLP_TRACES_HEADERS` to
`Authorization=Basic <base64(public-key:secret-key)>,x-langfuse-ingestion-version=4`.
Use `http://127.0.0.1:3000/api/public/otel/v1/traces` for host CLIs instead.
These standard OTLP variables are optional; unset means local spans are not
exported. The [Langfuse OTLP guide](https://langfuse.com/integrations/native/opentelemetry)
documents this endpoint, authentication, and v4 ingestion header. The API,
invoice worker, review worker, and graph demo have independent service names.
Workflow spans emit stable run, invoice, and trace identifiers plus error
types. The gateway adds one Langfuse `generation` or `embedding` observation
per logical model call with model, usage, latency, and a cost only if reported
by LiteLLM. Prompt and response content, documents, vectors, credentials, and
provider error messages remain excluded. The gateway boundary is used because
the operator's LiteLLM deployment is configured only by its direct URL, key,
and model names; [ADR 0009](../../adr/0009-gateway-boundary-llm-tracing.md)
records this decision. The [Langfuse attribute mapping](https://langfuse.com/integrations/native/opentelemetry)
defines the observation fields.

The observability profile does not configure or proxy LiteLLM. The invoice
worker continues to use only the operator's `LITELLM_API_BASE`,
`LITELLM_MASTER_KEY`, and task model-name variables.

Stop the optional services without deleting their named volumes:

```sh
docker compose --profile observability stop grafana prometheus langfuse-web langfuse-worker langfuse-clickhouse langfuse-redis langfuse-minio langfuse-postgres
```
