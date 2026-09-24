# Real Compose pipeline runner

Step 5.2 drives the deployed API, Postgres/MinIO stack, and durable invoice worker.
It does not call graph nodes directly, simulate API responses, or use any LiteLLM
proxy configuration file. Uploads and readback use persona tokens; worker model
traffic uses only `LITELLM_API_BASE`, `LITELLM_MASTER_KEY`, and model-name
variables from [`.env.example`](../../.env.example).

## Recorded smoke

With the direct LiteLLM URL, key, and `LITELLM_MODEL` present in `.env`:

```bash
uv run python -m eval.runners.run_pipeline --recorded
```

This uses the committed PNG for one development invoice (`SYN-CLEAN-0034`),
verifies its checksum against the golden label, starts Compose, seeds the golden ERP, uploads
through `POST /v1/invoices`, runs the real worker in a one-shot Compose
container, and reads invoice detail and auditor provenance through the API.
Three committed synthetic cassettes replay extraction, embedding, and triage
without a network fallback. The cassette transport keys on task alias and
preserves the configured model name in provenance; it never changes the
operator's LiteLLM route settings. Only in recorded mode, the worker sets
`LITELLM_EMBED_MODEL=recorded-embed-model` as a replay label. Regenerate and
verify the cassettes offline with:

```bash
uv run python -m eval.runners.build_smoke_cassettes
```

The smoke PNG is committed because the same pixels can have different PNG
checksums when native zlib encoders differ. The full golden builder records
its Pillow and zlib versions and rejects drift; recorded mode uses identical
document bytes on every CI host.

The current confidence gate routes this clean sample to human review, so the
smoke expects a `PAUSED` run and `NEEDS_REVIEW` invoice with a `triage.prepared`
event. Recorded mode is a pipeline smoke, not a model-quality measurement.

## Full live run

First build the 500 documents via [the golden builder](../../docs/EVALUATION.md).
Set an actual 384-dimensional embedding route through the existing
`LITELLM_EMBED_MODEL` variable in `.env.example`; the worker already requires
it. Then run:

```bash
uv run python -m eval.runners.run_pipeline --split all
```

Use `--split development` or `--split held_out` to run one split, and `--limit N`
for a bounded subset. The runner verifies all selected document bytes before
uploading any case. It keeps duplicate parents in the same selection and sends
them before their repeated documents. A confirmed exact duplicate is expected
to return HTTP 200 with the original IDs and an audited Reject event. Other
uploads require HTTP 201. Idempotency keys pin dataset version and sample ID,
so rerunning the same dataset does not create a second invoice. A live report
marks previously processed runs and exits nonzero so their near-zero replay
times cannot be mistaken for fresh latency measurements.

The runner starts Compose by default and seeds its 399 golden purchase orders
and receipts beside the existing base ERP fixture. `--no-start` uses an already
running Compose API. For an isolated run, set separate `API_PORT`,
`POSTGRES_PORT`, `MINIO_PORT`, and `MINIO_CONSOLE_PORT`, pass `--project-name`,
and point `EVAL_API_BASE_URL` to that local API port. The runner allows only a
loopback HTTP API address. It does not remove a Compose project or its volumes.

Reports default to ignored `eval/data/runs/` and contain one row per sample:
the upload result, upload/worker active latencies, worker route/error type,
invoice detail, and full auditor
provenance page. Worker errors still produce an evidence report and a nonzero
exit status. The next steps compute metrics and diagnostics from this report.
