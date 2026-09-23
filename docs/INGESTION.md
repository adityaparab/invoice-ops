# Invoice upload (step 1.1)

`POST /v1/invoices` accepts one multipart file named `file`, authenticated by
`Authorization: Bearer <INVOICEOPS_SERVICE_TOKEN>`. An `Idempotency-Key` is required. Authentication
happens before body reading or multipart parsing. PDF, PNG, and JPEG declared media types must match
their leading file signatures; this is format identification, not document decoding or malware scanning.

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
the same fingerprint returns the original `201` body without a second invoice, run, or ledger event;
different content under that key returns `409`. Only successful requests reserve a key.

**Staged duplicate behavior:** identical content under a new key currently returns `409`, enforced
by the unique invoice content hash. Step 1.3 will add the planned `200`, original invoice/run IDs,
`duplicate: true`, and audited Reject route. Email ingestion is separate step 1.2 work.

Raw objects live at `s3://<bucket>/sha256/<first-two-hash-characters>/<content-hash>`. Conditional
S3 puts preserve an existing object. Upload happens before acquiring database locks. The transaction
then takes a bounded idempotency-key advisory lock, rechecks replay, and atomically inserts the invoice,
queued run, `ingest.accepted` SYSTEM event, and original response. The event pins `ingestion-v1` and
explicit `not-applicable@v1` model, prompt, and policy versions. It records references, size, source,
and type, never raw bytes, filenames, tokens, or storage credentials.

Ledger or database failure rolls back all rows. An upload followed by a failed transaction may leave
an unreferenced content-addressed object; request cleanup never deletes shared objects. A retry reuses
that object. Database connect, statement, lock, and transaction deadlines are bounded. Storage calls
have a configurable 10-second deadline (`INVOICEOPS_STORAGE_TIMEOUT_SECONDS`) and no hidden retries.

Expected failures are `application/problem+json`: `400` empty/malformed multipart, `401` bad token,
`408` receive timeout, `409` key/content conflict, `413` byte limit, `415` unsupported/mismatched type,
and `503` unavailable or unconfigured infrastructure. Traces identify requests; successful commits
also log invoice/run IDs. Responses and application logs omit credentials and underlying service errors.

Compose runs a one-shot `storage-init` service to create `INVOICEOPS_RAW_BUCKET` (default
`invoiceops-raw`) before API startup. The standalone API needs `INVOICEOPS_POSTGRES_DSN` for the
restricted runtime role, `INVOICEOPS_MINIO_URL`, `INVOICEOPS_MINIO_ACCESS_KEY`,
`INVOICEOPS_MINIO_SECRET_KEY`, and `INVOICEOPS_SERVICE_TOKEN`. Provision the bucket explicitly with
`uv run python -m invoiceops_agent.tools.storage_bootstrap`; liveness stays available when uploads
are unconfigured. Local Compose uses synthetic MinIO root credentials; use bucket-scoped credentials
for a shared deployment. Database migrations remain on the separate owner connection.
