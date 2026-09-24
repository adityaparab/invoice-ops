# Invoice ingestion and exact duplicates (steps 1.1–1.3)

`POST /v1/invoices` accepts one multipart file named `file`, authenticated by
`Authorization: Bearer <INVOICEOPS_SERVICE_TOKEN>`. An `Idempotency-Key` is required. Authentication
happens before body reading or multipart parsing. PDF, PNG, and JPEG declared media types must match
their leading file signatures; this is format identification, not document decoding or malware scanning.

The React Intake screen sends this request with a separate in-memory service
token and shows transfer progress. It parses the `201` accepted and `200`
exact-duplicate responses at the Zod boundary, then uses Maria's distinct
analyst token to read the current invoice/run status. The original upload
response's `QUEUED` status is labeled as an ingest response, since processing
may advance after the upload commits.

```sh
curl --fail-with-body http://localhost:8000/v1/invoices \
  -H 'Authorization: Bearer invoiceops-local-upload-token' \
  -H 'Idempotency-Key: synthetic-upload-001' \
  -F 'file=@synthetic-invoice.pdf;type=application/pdf'
```

Use synthetic documents and replace the example token outside local development. The successful
`201` response contains `invoice_id`, `run_id`, `status: "QUEUED"`, and `duplicate: false`. The run
is recorded durably; step 1.1 does not start a worker, extraction, or the hello graph.

The API counts every multipart byte, including headers and boundaries, even without Content-Length.
Defaults are 10 MiB for the document (`INVOICEOPS_DOCUMENT_MAX_BYTES`), 11 MiB for the complete
request (`INVOICEOPS_UPLOAD_MAX_BYTES`), and 30 seconds to receive/parse/hash
(`INVOICEOPS_UPLOAD_TIMEOUT_SECONDS`). A request may contain no extra files or form fields.
Multipart headers per part are limited to 8 KiB. Starlette spools uploads to temporary files above
1 MiB; all spools close on success, error, timeout, or disconnect. The bounded document is read in
64 KiB chunks for incremental SHA-256 and assembled for the async S3 upload.

The request fingerprint includes source `UPLOAD`, declared document media type, and SHA-256 content
hash. Filenames and multipart boundary changes do not change request identity. A repeated key with
the same fingerprint returns the stored `201` or `200` body and status without another invoice,
run, or ledger event;
different content under that key returns `409`. Only successful requests reserve a key.

Identical content under a new key returns `200`, the original invoice/run IDs, and `duplicate: true`.
It appends one `ingest.duplicate_rejected` SYSTEM event with node `Reject`, `route: "REJECT"`, and
`reason: "DUP_EXACT"`. The event and that key's replay response commit in the same transaction.
Replaying the duplicate key returns its original `200` without another event. Different new keys
represent distinct duplicate attempts and each records one event. The shared document contract
carries the source independently of content identity.

The duplicate response refers to the original successful `201` ingestion, even if later runs exist.
Its `status: "QUEUED"` describes that original acceptance, not a current processing-status query.
Duplicate rejection never changes the original invoice/run status or creates another invoice/run.
The event uses the original run's graph-version pin and explicit `not-applicable@v1` pins for model,
prompt, and policy. If the original successful ingestion cannot be resolved, the request fails with
`503` and does not invent an identity or reserve a key.

Raw objects live at `s3://<bucket>/sha256/<first-two-hash-characters>/<content-hash>`. Conditional
S3 puts preserve an existing object. Upload happens before acquiring database locks. The transaction
then takes a bounded idempotency-key advisory lock, rechecks replay, and atomically inserts the invoice,
queued run, `ingest.accepted` SYSTEM event, and original response. New runs pin `invoice-v1` and
explicit `not-applicable@v1` model, prompt, and policy versions. It records references, size, source,
and type, never raw bytes, filenames, tokens, or storage credentials. A conflicting content-hash
insert waits for the creator's transaction; the duplicate path then locks the original invoice row
before appending its Reject event and saving the `200` response. Database uniqueness and these locks
make simultaneous new keys safe without retries or a second run.

Ledger or database failure rolls back all rows. An upload followed by a failed transaction may leave
an unreferenced content-addressed object; request cleanup never deletes shared objects. A retry reuses
that object. Database connect, statement, lock, and transaction deadlines are bounded. Storage calls
have a configurable 10-second deadline (`INVOICEOPS_STORAGE_TIMEOUT_SECONDS`) and no hidden retries.

Expected failures are `application/problem+json`: `400` empty/malformed multipart, `401` bad token,
`408` receive timeout, `409` key reuse with different content, `413` byte limit, `415` unsupported/mismatched type,
and `503` unavailable or unconfigured infrastructure. Traces identify requests; successful commits
also log invoice/run IDs. Responses and application logs omit credentials and underlying service errors.

Compose runs a one-shot `storage-init` service to create `INVOICEOPS_RAW_BUCKET` (default
`invoiceops-raw`) before API startup. The standalone API needs `INVOICEOPS_POSTGRES_DSN` for the
restricted runtime role, `INVOICEOPS_MINIO_URL`, `INVOICEOPS_MINIO_ACCESS_KEY`,
`INVOICEOPS_MINIO_SECRET_KEY`, and `INVOICEOPS_SERVICE_TOKEN`. Provision the bucket explicitly with
`uv run python -m invoiceops_agent.tools.storage_bootstrap`; liveness stays available when uploads
are unconfigured. Local Compose uses synthetic MinIO root credentials; use bucket-scoped credentials
for a shared deployment. Database migrations remain on the separate owner connection.

## Signed email stub

`POST /v1/invoices/email-webhook` accepts one JSON object containing an `attachment` with
`content_type` (`application/pdf`, `image/png`, or `image/jpeg`) and `content_base64`. It contains no
sender address, mailbox metadata, or provider-specific fields. This is a synthetic webhook source;
no email provider is connected. It uses `INVOICEOPS_WEBHOOK_SECRET`, independent of the upload Bearer
token. The caller sends `Idempotency-Key`, `X-Webhook-Timestamp` (Unix seconds),
`X-Webhook-Nonce` (16–128 allowed ASCII characters), and `X-Webhook-Signature` (lowercase hex
HMAC-SHA256). Compute the HMAC over the exact bytes:

```text
<timestamp>.<nonce>.<raw JSON request body>
```

The API bounds and reads the raw JSON before parsing it, checks signature equality in constant time,
then decodes one canonical base64 document and applies the same document-size and signature rules as
uploads. The timestamp must be within 300 seconds of receipt by default. The raw JSON limit defaults
to 14 MiB; the document remains limited to 10 MiB. Oversized input returns `413`, a bad or stale
signature returns `401`, malformed JSON/base64 returns `400`, and a type/signature mismatch returns
`415`. Both new and duplicate ingestions use the same `201`/`200` response contract as upload.

Every successful signed request consumes its nonce, including a fresh-nonce replay of an existing
idempotency key. Reusing a nonce returns `409`. A fresh nonce and the same key/body returns the stored
status and body without an extra audit event. Keys are shared with the upload endpoint; using one
across sources conflicts because source is part of the request fingerprint. The nonce claim, replay
check, invoice/run/ledger event, and successful response commit atomically. Invalid input and failed
storage or database work leave the nonce available for a valid retry. Shared content-addressed raw
objects are never deleted after a failed database transaction.
