# Email webhook — saved work in progress

Step 1.2 is incomplete and is intentionally saved on `p1/02-email-webhook` in
`/tmp/invoiceops-email`, with a remote backup on `origin`. It is not ready for a PR or merge. Work stopped at the user's requested
boundary on 2026-09-23; the plan checkbox remains open. The base is `770b570` (dataset step 1.8).
The dedupe prerequisite has since merged into `main` at `151deef` and still needs integration here.

## Implemented

- Deterministic HMAC-SHA256 verification of `timestamp.nonce.rawbody`, constant-time comparison,
  strict signature/header shapes, and an injected clock with a configurable five-minute window.
- Bounded raw JSON reception, actual-byte accounting for chunked input, receive timeout, duplicate
  header rejection, and signature verification before JSON parsing. Shared body-limit helpers also
  preserve multipart upload accounting.
- Minimal strict Pydantic and Zod attachment envelopes (`content_type`, `content_base64`), an
  OpenAPI schema helper, and optional environment-backed secret/window/body/timeout settings.
- Offline tests for signatures, freshness, malformed metadata/envelopes, duplicate fields,
  body bounds, timeout, secret validation, and the existing upload behavior.

No endpoint, base64 document decoder, database nonce claims, deployment wiring, or live model
calls have been added. No dependencies or migrations changed.

## Known issue and pending work

1. Add signed JSON regressions for excessive nesting and integers longer than Python's conversion
   limit. `json.loads` currently lets `RecursionError` and some `ValueError` failures escape. Map
   these to the typed 400 problem response narrowly around JSON parsing; do not hide programmer
   errors in authentication/configuration with a broad outer `ValueError` handler.
2. Merge current `main` to obtain step 1.3's source-aware request fingerprints and persisted
   `IngestionOutcome` status/body contracts. Preserve the user's deferred live-evaluation ToDo.
3. Decode strictly bounded base64 and reuse `read_document` for PDF/PNG/JPEG signatures, decoded
   size limits, and incremental hashing with source `EMAIL`. Expose the signed webhook route and
   required headers/body in OpenAPI; keep upload Bearer authentication unchanged.
4. Extend shared ingestion with an optional authenticated nonce receipt. Every successful request,
   including stored 201/200 replay, must insert its unused nonce in the same transaction as the
   successful ingestion/replay. A reused nonce returns 409. Fresh nonce plus the same key/body
   returns the stored status/body without an extra ledger event. Failed validation, storage,
   fingerprint conflict, ledger, or database work must leave the nonce reusable.
5. Do not take the upload path's early replay return for email. A read-only nonce/replay preflight
   may avoid unnecessary storage, but the bounded transaction must claim the nonce and recheck
   replay under the idempotency lock. Upload storage before the transaction when required; never
   delete shared content-addressed objects after rollback. Reuse existing `webhook_nonces` and
   the shared idempotency keyspace; cross-source requests with the same key conflict.
6. Add offline route/failure tests and real restricted-role PostgreSQL/MinIO coverage for fresh
   nonce replay, duplicate content, same/different-key races, raw retrieval, and nonce rollback.
   Wire synthetic configuration, document exact retry/signing semantics, and add a Compose CI
   stub smoke. Use an isolated Compose project/image/ports and clean only that project's volumes.
7. Run full checks, update user-facing ingestion/deployment docs and the step 1.2 plan entry only
   when complete. Do not claim extraction or real email-provider integration in this stub.

## Validation at save time

Ruff checks/format and strict mypy pass. The focused webhook/upload suite passes **65 offline
tests**, and the complete unit suite passes **315 offline tests**, with IP networking disabled.
New webhook database and Compose integration tests are pending because the
endpoint and transaction work are not implemented. The Zod schema has not been independently
typechecked in this WIP.

## Resume commands

```bash
cd /tmp/invoiceops-email
git status --short
git log -1 --oneline
git merge main
UV_CACHE_DIR=/tmp/invoiceops-uv-cache uv sync --locked
UV_CACHE_DIR=/tmp/invoiceops-uv-cache uv run ruff check .
UV_CACHE_DIR=/tmp/invoiceops-uv-cache uv run ruff format --check .
UV_CACHE_DIR=/tmp/invoiceops-uv-cache uv run mypy --strict
UV_CACHE_DIR=/tmp/invoiceops-uv-cache uv run pytest tests/unit --disable-socket --allow-unix-socket
```

Resume only after the user authorizes continued implementation. The saved branch is backed up remotely; open its PR only after the remaining implementation and
validation are complete.
