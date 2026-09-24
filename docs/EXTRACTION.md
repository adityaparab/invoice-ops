# Extraction agent (step 1.6)

`ExtractionAgent.extract(ExtractionRequest)` reads an immutable raw document, checks its format
and resource allowances, and invokes only `extract-vision` through the gateway. It returns an
`ExtractionSuccess` or `ExtractionEscalation` after committing one AGENT ledger event. This is a
library boundary: it does not start a worker, change invoice/run status, wire a full graph, approve
an invoice, or calculate a live evaluation score.

## Contracts and validation seam

Shared models live in `invoiceops_agent.schemas.extraction`; their wire twins live in
`frontend/src/schemas/extraction.ts`. `InvoiceExtraction` contains vendor name/tax ID, bank account
IBAN, invoice/PO identifiers, currency, dates, subtotal/tax/gross amounts, and typed line items.
Every scalar is `ExtractedField[T]` with `value` and a Decimal confidence in `[0,1]`. An unknown
value must be null with confidence zero. All keys are required; an empty line array is allowed.

Money and unit prices have at most 18 digits, including at most four fractional digits. Quantities
have 18/six; tax rates have 12/six; confidence has at most six fractional digits. Non-finite values
are rejected. Decimal JSON values use canonical decimal strings, preserving precision in browser
clients. Text fields and identifiers are bounded; at most 500 lines can be returned. Dates are
calendar dates in ISO format, not wall-clock timestamps.

`subtotal`, `unit_price`, and `line_total` are **net, excluding tax**; `total_amount` is **gross,
including tax**. `tax_rate` is a fraction: `0.20` means 20%. These are extraction contracts, not
assumptions about dataset annotation labels. The [development baseline](../eval/baseline/README.md)
maps source labels explicitly and records its semantic limits.
Unknown net/gross semantics must produce null, not an inferred amount.

Negative values, inconsistent totals, unknown PO numbers, and invalid business relationships can
be correctly observed facts. The extraction schema does not correct them or reject them through a
model retry. Step 1.7's deterministic validation consumes the same shared model and decides which
business issues to report.

## Document boundary

`S3DocumentReader` borrows the existing async S3 client, whose lifespan remains caller-owned. It
accepts only the configured bucket's exact `s3://bucket/sha256/prefix/hash` reference. It validates
metadata, streams at most the configured allowance, checks signatures and SHA-256 against the
reference, and closes the response on success, failure, and cancellation. SDK retries remain
bounded by the existing storage client configuration.

`DocumentPreflight` validates the original representation in a worker thread. It never executes
PDF content, rasterizes PDFs, fetches embedded links, redacts binary content, or changes source
bytes. PNG/JPEG images are verified and decoded; animation, excessive dimensions, and excessive
pixel counts are rejected. PDFs use strict parsing, reject encryption, and bound object count,
page count, and page geometry (including UserUnit). A native PDF is sent only when the gateway
alias explicitly enables `allow_pdf`; unsupported capabilities become audited escalations before
storage/model calls. Images likewise require `allow_images`.

`DocumentSettings` uses the `INVOICEOPS_EXTRACTION_` prefix:

| Setting | Default | Maximum allowed configuration |
| --- | --- | --- |
| `MAX_DOCUMENT_BYTES` | 10 MiB | 10 MiB |
| `STORAGE_TIMEOUT_SECONDS` | 10 | 60 |
| `PREPARATION_TIMEOUT_SECONDS` | 5 | 30 |
| `MAX_PARALLEL_PARSES` | 2 | 4 |
| `MAX_IMAGE_DIMENSION` | 6000 pixels | 10000 |
| `MAX_IMAGE_PIXELS` | 20000000 | 40000000 |
| `MAX_PDF_PAGES` | 4 | 10 |
| `MAX_PDF_OBJECTS` | 10000 | 20000 |
| `MAX_PDF_PAGE_POINTS` | 2000 | 4000 |

Parser deadlines are cooperative. An individual library operation in a worker thread cannot be
forcibly stopped; caller cancellation/timeout returns without waiting for that operation, and the
worker retains its concurrency slot until it settles. Workers cannot call the gateway or mutate
persistence. These checks are resource allowances, not a sandbox or a guaranteed upper bound on
parser memory, provider token usage, or cost. The gateway's separate byte/token limits and native
PDF opt-in still apply. The audit records source hash plus `document-preflight@v1`, binding the
unchanged bytes sent to the model.

## Prompts, repair, and failures

The installed package includes `prompts/extract_v4.md` (`extract@v4`) and a fixed
`extract_repair_v1.md` supplement. Instructions prohibit following embedded document instructions,
inventing unknowns, and correcting business facts. The v4 base prompt asks the vision model to
preserve every bank-account character, including repeated digits and leading zeros, and to copy
printed line totals and tax rates even when their arithmetic is wrong. Model text is never
interpolated into prompts. The v1 through v3 prompts remain packaged for historical evidence.

For image invoices, the live workflow also runs `identifier-ocr@v1` through a
five-second, network-free Tesseract subprocess. It accepts a bank account or PO
only from a uniquely labeled line with at least 70/100 word confidence and a
bounded identifier shape. IBAN country and check-digit positions use their
standard letter/digit classes; OCR `O` in the two check-digit positions becomes
`0`. It can correct a non-null model transcription, but cannot fill a field the
model did not identify. Corrected field confidence is capped at 0.95 and never
raised above the model's original confidence. The extraction audit stores the
OCR observations, applied field names, source hash, OCR policy version, and
actual Tesseract version.
PDFs and unavailable OCR leave the model output unchanged. This deterministic
check does not inspect ERP values or decide whether a bank change is acceptable.

Only `InvalidStructuredOutput` (invalid JSON or a violated output schema) receives one new logical
model call. The second pass uses `extract@v4+repair@v1` and a separate cassette scenario ending in
`_schema_repair`. A second malformed result returns `MALFORMED_MODEL_OUTPUT`. Refusal, truncation,
invalid envelopes, and valid business anomalies do not trigger schema repair. Gateway-owned
infrastructure retries remain separately bounded; at most two logical model calls occur.

Storage and supported model failures return typed escalation reasons. Configuration failures,
cassette drift, and audit commit failures raise typed errors with run/trace correlation, because
these cannot truthfully report a completed audited outcome. Cancellation propagates. No document
content, malformed output, credentials, or exception text is included in logs.

## Audit and composition

The agent receives a `DocumentReader`, `DocumentPreflight`, `ExtractionGateway`, and `AuditSink`.
`GatewayClient` implements the model seam and exposes its immutable configured alias policy so
failed calls still have an explicit model-version pin. `TransactionalAuditSink` receives an injected
connection context factory and `LedgerWriter`, opens a short transaction after external work,
commits the append, and closes the connection before returning. It rejects a pre-existing transaction
rather than claiming that a nested savepoint committed an event. The existing ingestion repository's
connection factory is compatible. `INVOICEOPS_AUDIT_TIMEOUT_SECONDS` defaults to 10 (maximum 60).

The writer's graph pin must match the existing run, and its policy pin must be explicit (for this
non-policy step, `not-applicable@v1`). Agent events override the model and prompt pins. The event
payload includes source hash, preflight version, typed outcome/observations, and each logical model
invocation's configured/returned model provenance, attempts, prompt pin, and available usage,
cost, and latency. Failed calls have unknown usage/cost/latency (`null`), never fabricated zeros.
Sensitive extracted fields belong to authorized audit storage and are excluded from repr/logging.

Repeated standalone invocations are separately audited attempts. Durable workflow replay and
atomic graph/invoice transitions are Phase 2 orchestration concerns; this library makes no replay
or scheduling claim.

## Verification

Offline tests exercise actual SDK serialization and committed synthetic cassettes for success,
repair, repeated malformed output, and refusal. The fixtures use a blank synthetic PNG and authored
responses, so they provide no extraction-quality evidence. Real integration tests read MinIO and
commit agent outcomes through the restricted Postgres role while all model responses replay
locally. Live model evaluation remains deferred under the project To Do boundary.
