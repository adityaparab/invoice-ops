# InvoiceOps Architecture

This document is the contract for component boundaries, workflow topology, HTTP resources, and the
PostgreSQL system of record. Implementation details may evolve only when this document and the
relevant ADR change together.

## 3. Orchestration

The primary workflow is:

`Ingest -> Extract -> Validate -> Match3Way -> Policy -> Gate -> AutoApprove -> Archive`

Duplicates route from Ingest to Reject. Extraction escalations and outcomes that fail the policy or
gate route to ExceptionTriage and pause at HumanReview before Archive. Nodes checkpoint after every
transition and remain idempotent under replay. The [composite confidence gate](CONFIDENCE_GATE.md)
records its three terms, threshold, versioned configuration, and evidence fingerprints; see
[worker operation and configuration](INVOICE_WORKFLOW.md).
The one-shot worker applies [bounded infrastructure retry and an audited dead-letter queue](RETRY_AND_DLQ.md)
using `runs.status=FAILED` plus sanitized retry metadata. Business decisions are never retried.

LangGraph's managed checkpoint tables live in the isolated `langgraph` schema so they do not
collide with the application-facing `public.checkpoints` projection. The saver uses a restricted
MessagePack serializer, and a completed thread is returned without re-executing nodes when the same
`run_id` is submitted again.

## 4. Architecture decisions

The decision records are the authority for why these cross-cutting constraints exist. Changes to a
decision require a superseding ADR or an explicit status change.

| ADR | Accepted decision |
|---|---|
| [0001](../adr/0001-deterministic-matcher-policy.md) | Keep matching and policy deterministic; reserve model reasoning for unstructured interpretation and triage. |
| [0002](../adr/0002-langgraph-primary-adk-variant.md) | Use LangGraph as the primary orchestrator and build an ADK comparison variant in Phase 6. |
| [0003](../adr/0003-composite-confidence-gate.md) | Combine extraction, match, and policy signals in a versioned gate that abstains below its threshold. |
| [0004](../adr/0004-append-only-ledger.md) | Preserve decision history in an append-only ledger with point-in-time version pins. |
| [0005](../adr/0005-gateway-only-model-traffic.md) | Route every model call through the gateway client and LiteLLM virtual aliases. |
| [0006](../adr/0006-synthetic-data-anomalies.md) | Use reproducible synthetic data with seeded anomalies and published prevalence assumptions. |
| [0007](../adr/0007-vcr-cassettes.md) | Replay committed model-response cassettes in tests; reserve live calls for explicit evaluation runs. |

## 5. HTTP API

The API is versioned under `/v1`. Health endpoints remain unversioned. Mutating endpoints require a
validated `Idempotency-Key`; API failures use RFC 7807 problem details. Authentication and persona
RBAC are dependency-injected at the transport boundary.

`POST /v1/invoices` accepts service-token-authenticated PDF, PNG, and JPEG multipart uploads. It
validates the declared type against the document signature, enforces a configurable byte limit,
stores the raw document at `sha256/{prefix}/{content_hash}` in MinIO, and atomically creates the
invoice, queued run, initial ledger event, and replay response. Reusing an idempotency key with the
same request returns the original body and HTTP status (`201` or `200`); reuse with different
content returns `409`.

SHA-256 is computed incrementally from the multipart spool. Steps 1.1 and 1.3 implement this
authenticated upload and replay contract; see [upload setup and limits](INGESTION.md).
Identical content submitted under a new
idempotency key returns `200` with the original invoice and run identifiers plus `duplicate=true`;
it creates no second invoice or run. The original invoice row is locked while an
`ingest.duplicate_rejected` SYSTEM event is appended with `route=REJECT`, making concurrent
duplicates race-safe and auditable. The event and its `200` replay response commit atomically;
replaying that key emits no additional event. Original processing statuses remain unchanged.

Step 1.2's `POST /v1/invoices/email-webhook` accepts a JSON stub email envelope. Authentication is
`HMAC-SHA256(secret, "{unix_timestamp}.{nonce}." + raw_body)` in `X-Webhook-Signature`, with the
timestamp and nonce carried in their corresponding `X-Webhook-*` headers. The body is bounded
before parsing, signatures use constant-time comparison, timestamps have a configurable five-minute
window, and successfully consumed nonces are unique in PostgreSQL. The decoded attachment reuses
the same content-addressed ingestion transaction with source `EMAIL`.

## 6. Data model

PostgreSQL stores operational state and audit history; MinIO stores immutable raw documents by
content hash.

| Table | Purpose | Important constraints/indexes |
|---|---|---|
| `vendors` | Synthetic vendor master | unique `external_id` |
| `purchase_orders` | PO header plus JSONB lines | unique `po_number`; vendor/status index |
| `goods_receipts` | Receipt header plus JSONB lines | unique receipt; PO/received index |
| `invoices` | Invoice read model and extraction | unique `content_hash`; status/created index; 384-dimension HNSW cosine embedding index |
| `ingestion_requests` | Durable cross-source idempotency claims and original responses | primary-key idempotency key; request hash |
| `webhook_nonces` | Consumed authenticated email webhook nonces | primary-key nonce; signed timestamp |
| `invoice_lines` | Normalized extracted lines | unique invoice/line number; Decimal-safe numeric columns |
| `runs` | Workflow execution | invoice/started index; graph and trace version pins |
| `checkpoints` | Serializable node snapshots | unique run/sequence |
| `ledger` | Append-only events | unique run/sequence; actor and non-null graph/model/prompt/policy pins |
| `exceptions` | Human-review queue | status/SLA index; JSONB evidence and recommendation |
| `decisions` | Append-only human decisions | unique idempotency key; action, rationale, reason, and version pins |

Money uses `numeric(18,2)`, quantities use `numeric(18,4)`, tax rates use `numeric(9,6)`, and all
timestamps are timezone-aware. The initial migration chooses HNSW over IVFFlat because HNSW serves
accurate nearest-neighbor queries without a training phase and performs well as the corpus grows
incrementally.

`ledger` and `decisions` reject `UPDATE`, `DELETE`, and `TRUNCATE` through always-enabled statement
triggers, including empty matches and owner writes. The runtime
`invoiceops_app` role receives only `SELECT`/`INSERT` grants on these tables; migrations use a
separate owner connection. Repositories mirror this boundary by exposing only append and read
methods. The ledger writer resolves all four version pins from environment-backed configuration,
while allowing an agent or policy event to override the relevant component version. The reader
uses bounded keyset pagination for both run-scoped and cross-run invoice histories, so trace and
provenance endpoints can render a complete history without per-entry queries.

## 7. LLM gateway boundary

Only `src/invoiceops_agent/gateway_client/` talks to the OpenAI-compatible LiteLLM endpoint. Callers
use the configured virtual aliases `extract-vision`, `triage-reasoner`, `eval-judge`, and `embed`;
an unknown alias fails before network I/O. The client redacts configured PII patterns, rejects
configured prompt-injection heuristics, and estimates text input plus requested output against each
alias's token budget before spending.
Binary assets are opt-in, byte/count bounded, and charged a configured token allowance; trusted
preprocessing must enforce model-specific dimensions/page counts because that allowance is not a
guaranteed provider-token bound. Text guards do not inspect binary document contents.

The transport disables SDK retries so the wrapper owns the retry contract: connection failures,
timeouts, transient rate limits, and 5xx responses receive bounded exponential backoff within one
total deadline. Valid Retry-After hints are honored or the retry is declined; quota/billing errors,
malformed structured output, and other request failures escalate immediately as typed errors.
Pydantic validates the returned JSON against the caller's response model. Results preserve configured
model-version pins and the gateway-reported model; virtual aliases alone are not immutable model
pins. A telemetry protocol exposes one sanitized outcome with alias,
prompt/model versions, latency, usage, optional cost, attempts, and validation status; Phase 4 adapts
it to OpenTelemetry spans. Local schema validation is mandatory, while stricter wire formats are
explicitly enabled per alias for backend compatibility.

Tests use committed JSON cassettes keyed by alias, scenario, and prompt version. Each cassette also
pins a hash of the fully guarded request and response schema, so prompt drift fails deterministically.
The recording transport uses create-only writes and never overwrites an existing cassette.
See [the gateway client contract](GATEWAY_CLIENT.md) for configuration, binary guard limits,
provenance, typed errors, and offline test transport usage.

## 8. Extraction agent

The extraction agent reads the immutable `raw_ref` through a bounded async MinIO adapter, converts
the document to a data URL, and invokes only the gateway's `extract-vision` alias. Its system prompt
is a packaged `extract_v1.md` artifact identified as `extract@v1`; prompt text is never assembled in
component code.

`InvoiceExtraction` represents vendor identity, IBAN, invoice/PO identifiers, currency, amounts,
dates, and typed line items. Every scalar field carries a Decimal confidence in `[0, 1]`, and a null
value must have confidence zero. One malformed structured response receives a schema-level retry;
repeated malformed output becomes a typed `MALFORMED_MODEL_OUTPUT` result rather than escaping into
the graph. Refusals and valid business anomalies do not trigger schema repair. Success and escalation
both commit an AGENT ledger event with prompt/model pins, source hash, preflight version, and available
call metrics. The actor-agnostic `TransactionalAuditSink` opens a short transaction after external work
and returns only after commit. See [the extraction contract](EXTRACTION.md) for native PDF opt-in,
parser limits, net/gross conventions, and the deterministic validation seam.

The standalone Validate node consumes that neutral extraction contract and applies pure, versioned
required-field, regular-invoice sign, line-math, subtotal, per-line tax, and gross-total checks.
Missing operands remain typed issues; unknown currencies never inherit an assumed rounding scale.
Both PASS and FAIL commit a POLICY ledger event with the complete policy, its fingerprint, the
extraction fingerprint, and Decimal evidence before returning. The one-shot worker connects the
nodes and replays committed evidence after a checkpoint interruption. See
[validation rules and contracts](VALIDATION.md).

## 9. Testing

Unit tests are deterministic and offline. Integration tests apply the real migration chain to a
clean pgvector Testcontainer and replay recorded model responses. Evaluation runs are the only tests
permitted to contact a live model.

## 10. Deployment

The root `compose.yaml` starts the API, Postgres with pgvector, and MinIO. An optional `gateway`
profile starts the Compose LiteLLM proxy when a native developer gateway is not used. The API waits
for healthy infrastructure, runs as a non-root user, and exposes dependency readiness separately
from process liveness. Postgres and MinIO persist in named volumes; published ports bind to localhost.
The one-shot `migrate` service applies owner-driven Alembic migrations and provisions the restricted
runtime login before the API or seed service starts. Only that service receives the owner DSN; the
API and [synthetic ERP seed service](SYNTHETIC_ERP.md) use `invoiceops_app`.
See [local platform setup](../deploy/README.md).
