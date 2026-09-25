# Invoice workflow worker

Step 2.6 connects an accepted invoice run to the `invoice-v1` LangGraph workflow. The graph
checkpoints synchronously after Ingest, Extract, Validate, Match3Way, Policy, Gate, and each chosen
terminal or review node. A run uses its existing `run_id` as the checkpoint thread ID. Repeating a
completed or paused run returns its checkpoint without another model call or audit event. If an audit
event committed before its checkpoint, the worker reads that event and checks its input fingerprint
before continuing.

New uploads pin `invoice-v1`; already queued `ingestion-v1` runs remain readable and keep their
original ledger version pin. The upload API creates queued runs. Start a one-shot worker for an
accepted run with its returned `run_id`:

```bash
docker compose run --rm invoice-worker invoiceops-invoice-run <run_id>
```

For native development, set the restricted `INVOICEOPS_POSTGRES_DSN`, owner-capable
`INVOICEOPS_CHECKPOINT_DSN`, and MinIO endpoint/credentials, then run
`uv run invoiceops-invoice-run <run_id>`. The checkpoint connection creates only LangGraph's
isolated `langgraph` schema. The operational connection uses the restricted application role.

The worker reads `LITELLM_API_BASE`, `LITELLM_MASTER_KEY`, `LITELLM_MODEL`,
`LITELLM_EMBED_MODEL`, and optionally `LITELLM_EXTRACT_MODEL` and `LITELLM_TRIAGE_MODEL` from the environment. It does not
read a LiteLLM proxy configuration file. `LITELLM_EMBED_MODEL` must name a route that returns one
384-dimensional vector. If either optional model name is empty, that task uses `LITELLM_MODEL`.

For the ADK variant, set `INVOICEOPS_WORKFLOW_ENGINE=adk` and
`LITELLM_ADK_MODEL` to a Gemini model alias served by the same LiteLLM URL and key. ADK routes
the same typed state through its graph engine and stores pause/resume events in the isolated
PostgreSQL `adk` schema. Extraction and triage use `LITELLM_ADK_MODEL` through the existing gateway
client; embeddings retain `LITELLM_EMBED_MODEL`. The default engine remains LangGraph. Use the same
one-shot worker and review commands; run IDs and review decisions keep their API contract.

The [composite gate](CONFIDENCE_GATE.md) now combines observed field confidence, normalized
three-way match delta, and policy severity at a versioned threshold. A policy finding that requires
review still overrides the score. Set `INVOICEOPS_AUTO_APPROVAL_ENABLED=false` to disable automatic
approval during an operational hold. Exception triage prepares deterministic evidence and pauses at
HumanReview. The [exception decision API](EXCEPTION_DECISIONS.md) records analyst proposals and
independent manager signoff. Its one-shot worker resumes the checkpoint, and repeating the same
accepted decision does not repeat the human review or archive event.

The [triage agent](TRIAGE_AGENT.md) drafts cited recommendations through the `triage-reasoner`
alias. It uses the deterministic exception taxonomy and policy evidence, and records its model and
prompt pins with the review queue projection.

The invoice graph has a 420-second default deadline and a 450-second configuration maximum.
It covers the bounded extraction,
embedding, and triage gateway calls plus checkpoint and audit work; the gateway still
bounds each logical call to 120 seconds. A timeout marks the run failed for audited
redrive rather than pretending a partial graph is complete.

[Retry and dead-letter operation](RETRY_AND_DLQ.md) records run status, retries only uncaught
infrastructure failures, and supports audited operator redrive.
