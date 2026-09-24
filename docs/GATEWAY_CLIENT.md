# Gateway client

Step 1.5 adds an async OpenAI SDK client for the configured LiteLLM `/v1` endpoint. The client has
no default endpoint or key, never follows redirects, and disables environment proxy discovery.
Only the configured subset of `extract-vision`, `triage-reasoner`, `eval-judge`, and `embed` is
available. The first three use `complete`; `embed` uses `embed`. Provider model names are rejected
as request aliases before HTTP I/O. The explicit gateway URL is a trusted deployment setting;
URL syntax checks cannot prove that an arbitrary hostname is actually a LiteLLM deployment.

## Sensitivity routing, fallback, and cache

Calls are `restricted` by default. Each task alias sends its configured `LITELLM_*_MODEL` name;
an explicitly `public` request may use that task's optional `*_PUBLIC_MODEL` name. Optional
`*_FALLBACK_MODEL` and `*_PUBLIC_FALLBACK_MODEL` names form a primary/fallback route chain.
Restricted requests never use public model names. These are model-name variables in `.env.example`
and Compose; the application does not load a LiteLLM proxy YAML or another LiteLLM settings source.
Fallback starts only after bounded connection, timeout, 408/429, or 5xx retries on the primary
route. Quota/billing denial, guard rejection, invalid model output, and other business failures
escalate without fallback. The total deadline covers every route. Successful provenance and traces
pin the route that actually answered. See [ADR 0010](../adr/0010-public-cache-and-gateway-hardening.md).
The internal logical-call deadline is 120 seconds, with each provider attempt
bounded to 40 seconds. This gives a slow primary more time to answer. Retry
stops early when another attempt would leave less than one full attempt for a
named fallback; routes without fallback retain up to three attempts. These are
application retry limits, not additional
LiteLLM proxy configuration or environment variables.

Semantic caching requires `semantic_cache=True`, `sensitivity="public"`, a `public_` scenario,
text-only content, and an injected cache store. No invoice workflow request opts in. The gateway
embeds the guarded text through the configured LiteLLM embedding model and searches a versioned
Postgres pgvector cache at cosine similarity ≥0.995. The namespace pins task, scenario, prompt
version, response schema, primary and embedding model names, and exact numeric anchors. The table
also separates embedding dimensions; recognizable PII in the request or response bypasses storage.
The table stores embeddings and validated public responses. It has no columns for prompts, invoice
documents, or credentials. Entries expire after one day and each namespace is bounded to 10,000 entries. Cache
lookup/storage failures fall back to the normal model call. A cache hit has `attempts=0`, zero
completion tokens, no completion cost, and `cache_hit=True`; the embedding lookup still incurs its
own measured model cost. Approximate reuse is suitable only when the caller can guarantee a
public, stable-answer question; it is deliberately unavailable for invoice decisions.

The gateway accumulates observed `x-litellm-response-cost` values per run with `Decimal` and emits
one structured warning and a Prometheus alert event when the observed total reaches $0.04. This
is an advisory signal scoped to a gateway client, not a hard spend limit: missing cost headers,
infrastructure failures, and another worker's calls cannot be counted. Enforce hard key or team
budgets in the operator's LiteLLM deployment. The optional Prometheus stack has a rule named
`InvoiceOpsObservedRunBudgetExceeded` for the event.
Response cost is read before SDK envelope parsing, so a billed invalid envelope
retains its observed cost in the failure audit. A response without usable cost
headers remains unknown and cannot satisfy the evaluation cost-coverage gate.

The workflow reads only `LITELLM_API_BASE`, `LITELLM_MASTER_KEY`, `LITELLM_MODEL`, and
task-specific `LITELLM_*_MODEL` names from `.env`. `GatewaySettings` is an explicitly constructed
value object; it does not load another environment prefix or proxy configuration file.
The eval-only triage judge constructs the `eval-judge` alias from the explicit
`LITELLM_JUDGE_MODEL` name and the same direct URL/key; it does not run unless
that optional name is supplied. Each internal alias has a `model_version` pin
supplied from the selected model name. That name may be a
mutable LiteLLM route rather than an immutable provider revision; operators should keep deployment
records for fallback and route changes.
The response also preserves the gateway-reported `model` string; the client cannot independently
attest the actual provider revision behind that string.
An optional `model_name` sends a specific LiteLLM route while callers continue to use the
application alias. Without it, the alias itself is sent. `model_name` can be a mutable route name;
the configured `model_version` pin and recorded gateway response should be interpreted accordingly.

An alias policy controls `input_token_limit`, `output_token_limit`, `total_token_limit`,
`binary_token_reserve`, `allow_images`, `allow_pdf`, and `response_format`. The default response
format is `text` for compatibility with native/Ollama routes. Callers must instruct the model to
return JSON. Enable `json_object` or `json_schema` only for aliases that support that wire format;
strict schema mode sends the caller's schema unchanged, so it must satisfy the backend's supported
JSON Schema subset. Every response is locally validated with `model_validate_json(..., strict=True)`
regardless of the wire format. Refusal, truncation, tool calls, missing usage, malformed envelopes,
and schema failures raise `InvalidGatewayResponse` without a gateway retry. Malformed JSON/Pydantic
output uses the narrow `InvalidStructuredOutput` subclass, allowing the extraction agent to apply its
one explicit schema-repair pass without retrying refusals or other response failures.
`configured_policy(alias, context)` exposes the immutable alias policy for preflight and audit pins.

```python
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from invoiceops_agent.gateway_client import (
    GatewayClient,
    GatewayMessage,
    GatewayRequest,
    TextPart,
)
from invoiceops_agent.agents.near_duplicate_settings import LiteLLMWorkflowSettings


class ExtractedTotal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    total: Decimal


async def extract_total(run_id: UUID, trace_id: str) -> ExtractedTotal:
    request = GatewayRequest(
        alias="extract-vision",
        run_id=run_id,
        trace_id=trace_id,
        prompt_version="extract@v1",
        messages=(
            GatewayMessage(
                role="user",
                content=(TextPart(text="Return JSON for synthetic invoice total 123.45."),),
            ),
        ),
    )
    async with GatewayClient(LiteLLMWorkflowSettings().gateway_settings()) as client:
        result = await client.complete(request, ExtractedTotal)
    return result.value
```

A long-lived client should be shared by callers and closed at application shutdown. Each result
contains the validated value, alias/model/prompt provenance, token usage, attempt count, latency
in milliseconds, and optional `Decimal` USD cost from `x-litellm-response-cost`. Missing cost is
unknown, not zero. `EmbeddingRequest` carries the same run/trace/prompt context plus an ordered tuple
of input strings; its result validates finite, consistently sized vectors and input ordering.

## Guards and limits

Configured regular expressions redact PII in all message text and embedding inputs before HTTP
serialization. Defaults cover email addresses, international phone patterns, and IBAN patterns.
Configured injection heuristics reject matching user text and embedding inputs before spending;
system and assistant instructions are trusted application inputs. Guardrails are deterministic
heuristics: they can miss variants or flag legitimate text. They are not a complete injection or
PII detection system. A redacted value is not automatically reconstructed in model output.

`ImagePart` supports PNG/JPEG/WebP/GIF base64 data URLs; `FilePart` supports PDF data URLs and fixes
the transport filename to `invoice.pdf`. Remote URLs and arbitrary filenames are rejected.
Both binary formats are opt-in per alias. The client checks base64 syntax, per-asset byte limits,
per-request asset count, and aggregate request size. For both chat and embeddings,
`max_request_bytes` bounds the exact SDK-serialized HTTP body, including JSON escaping and the
request envelope. The check runs before the transport can contact the gateway. It does not decode
images, parse PDF pages,
inspect binary PII, or detect instructions inside documents. Trusted preprocessing must validate
content signatures, dimensions/page counts, and any required binary redaction before enabling
binary traffic. A data URL's declared MIME type is not proof of its content.

Text accounting uses UTF-8 byte counts plus schema and message overhead as a conservative estimate
for ordinary tokenizers. Each binary part charges the configured `binary_token_reserve`. That
reserve is an allowance, **not a proven upper bound** for arbitrary image dimensions or PDF page
counts. Set it alongside model-specific preprocessing limits. No local estimate guarantees the
provider's actual input-token charge or cost; enforce spending limits at LiteLLM too. Returned
usage exceeding configured limits is rejected, although the provider call has already occurred.

## Retries, cancellation, and telemetry

The wrapper owns retries; SDK retries are disabled. Connection failures, request timeouts, HTTP
408/429, and 5xx responses receive bounded exponential backoff. Known quota/billing/budget failures,
other 4xx responses, rejected guards, and malformed/schema/business responses escalate immediately.
A valid `Retry-After` number or HTTP date is honored; the client declines to retry if that delay
exceeds its configured delay cap or remaining deadline. Invalid hints use bounded local backoff.
Attempts, request timeout, retry delays, and total elapsed time are all bounded. Cancellation
propagates immediately and never retries. The OpenAI SDK has `max_retries=0`, leaving this wrapper
as the only application retry layer. The wrapper does not promise model-call idempotency: a lost response
can lead to a charged retry, and repeated calls from the application remain separate invocations.

Errors expose stable typed categories, run ID, trace ID, and attempt count without SDK exception
text, response bodies, URLs, keys, or document content. SDK debug payload logging is disabled by
the doorway. `GatewayTelemetry.record` receives one sanitized outcome for a configured call,
including guard failures and cancellation. It annotates one OpenTelemetry generation or
embedding span per logical call with model, version, token, latency, cost, and typed failure
metadata. The span is a child of the workflow tool span when called from the invoice graph.
Langfuse receives it through the same optional OTLP exporter as workflow spans. Prompts,
outputs, document bytes, embedding vectors, credentials, and provider error messages are
excluded; see [ADR 0009](../adr/0009-gateway-boundary-llm-tracing.md).
Telemetry implementations must be nonblocking and must not throw. Application/ledger adapters
consume these metadata hooks; the gateway itself does not own a database transaction.

Clock, UTC clock, async sleep, telemetry, and HTTP transport are injectable. The pinned
`openai==3.19.0` SDK uses `httpx2==2.13.1`; API readiness probes continue using `httpx` independently.

## Offline cassettes

`CassetteTransport(directory)` replays committed JSON with no network fallback. Identity is
alias + scenario + prompt version. A SHA-256 hash binds the guarded serialized request and response
schema; run/trace IDs and authorization headers are excluded. Prompt or schema drift fails with
`GatewayCassetteMismatch`. Missing files never trigger recording automatically.

Recording requires explicit `mode="record"` and an explicitly supplied upstream transport.
`GatewayClient` scopes each logical call, so a format-version-2 cassette stores its ordered outcomes
across retries, including recovered calls and exhausted retry budgets. Outcomes contain response
JSON and a small metadata-header allowlist, or a sanitized connection/timeout failure category.
A replay starts at the first outcome for every logical call, even when calls share a client or run
concurrently. Extra or unconsumed outcomes reject retry-policy drift. Legacy single-response
fixtures still replay unchanged. Retry delays are recomputed using the caller's retry settings;
freeze the injected UTC clock when testing absolute-date `Retry-After` headers.

A recorder reserves the scenario before upstream I/O. Concurrent recorders of the same scenario
fail before spending; different scenarios remain independent. At logical-call completion, it
atomically publishes one complete sequence with a create-only filesystem link. Existing fixtures
are never overwritten, including files created concurrently. Closing a transport with active
calls is rejected. Cancellation or deadline interruption discards an incomplete recording and
releases its reservation. Cancellation waits for any already-started local filesystem operation
to settle; an atomic publication already in progress may leave a complete fixture. HTTPX timeout
errors can be replayed; an attempt cancelled by the wrapper's timer discards the recording because
the cassette does not reproduce elapsed network time. A process crash can leave a `.recording`
reservation; remove it only after verifying no recorder is active.

Only the guarded request hash is stored, never request contents or authentication. **Response bodies may
contain sensitive data:** record only approved synthetic fixtures. Production responses must not
be committed. New prompt versions require new cassettes; do not overwrite a cassette to make a
test pass. The initial fixture is an authored synthetic HTTP response recorded through a mock
transport, not evidence of live model quality. Unit tests have all Internet sockets disabled.

The retry and validation contract follows the official
[rate-limit guidance](https://developers.openai.com/api/docs/guides/rate-limits),
[error classification](https://developers.openai.com/api/docs/guides/error-codes), and
[structured-output guidance](https://developers.openai.com/api/docs/guides/structured-outputs).
