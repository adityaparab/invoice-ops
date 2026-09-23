# LiteLLM virtual model routes

InvoiceOps uses `extract-vision`, `triage-reasoner`, `eval-judge`, and `embed` regardless of
provider. These files configure the optional Compose `litellm` service. They contain no provider
credentials or deployment URLs; LiteLLM resolves `os.environ/NAME` at startup.

| Configuration | Upstream | Required environment variables |
| --- | --- | --- |
| `config.yaml` (default) | Developer's native LiteLLM proxy | `LITELLM_MASTER_KEY`, `NATIVE_LITELLM_BASE_URL`, `NATIVE_LITELLM_API_KEY` |
| `config.ollama.yaml` | Developer's Ollama server | `LITELLM_MASTER_KEY`, `OLLAMA_BASE_URL`, `OLLAMA_EXTRACT_MODEL`, `OLLAMA_TRIAGE_MODEL`, `OLLAMA_JUDGE_MODEL`, `OLLAMA_EMBED_MODEL` |
| `config.prod.yaml` | OpenAI | `LITELLM_MASTER_KEY`, `OPENAI_BASE_URL`, `OPENAI_API_KEY` |

Set the selected values in the untracked root `.env`, or supply them through the deployment
environment. `LITELLM_MASTER_KEY` authenticates callers of this Compose gateway; the upstream API
key is a separate credential and stays in the gateway container. The application receives only
the selected gateway URL and gateway credential when the client is added in step 1.5.

`preflight.py` checks only the file selected by `--config`, recursively validates complete
`os.environ/NAME` references, and rejects missing or whitespace-only values before LiteLLM starts.
Failures log a structured error code and variable names; values, paths, and parser messages stay
out of the logs. This check is necessary because LiteLLM itself can resolve absent references to
`None`. On success, the preflight process replaces itself with LiteLLM and preserves every argument.

## Select a configuration

`LITELLM_CONFIG` is a filename within this directory and defaults to `config.yaml`. Run commands
from the repository root:

```bash
LITELLM_CONFIG=config.yaml docker compose --profile gateway config --quiet
LITELLM_CONFIG=config.yaml docker compose --profile gateway up -d --wait litellm
```

Use `config.ollama.yaml` or `config.prod.yaml` in the same commands to switch the upstream. The
gateway is reachable at `http://litellm:4000/v1` from Compose services and at
`http://127.0.0.1:4001/v1` from the host by default; `LITELLM_PORT` changes the host port. The API
does not depend on this optional service. Its gateway client is implemented separately in step 1.5.

The Compose service must mount the selected file and preflight script read-only, use Python as
its entrypoint, and forward the selected provider's environment variables:

```yaml
entrypoint: ["python", "/app/preflight.py"]
command: ["--config", "/app/config.yaml", "--port", "4000", "--host", "0.0.0.0"]
volumes:
  - ./deploy/litellm/${LITELLM_CONFIG:-config.yaml}:/app/config.yaml:ro
  - ./deploy/litellm/preflight.py:/app/preflight.py:ro
```

The pinned LiteLLM image already supplies PyYAML; it is only a development dependency of the
application repository for offline preflight tests. Keep unused provider variables optional in
Compose: the preflight enforces the requirements of the selected configuration at runtime.

## Native developer proxy

The default bridge maps the application aliases to the native names already specified by the
project:

| Application alias | Native upstream alias | Endpoint type |
| --- | --- | --- |
| `extract-vision` | `glm-ocr` | Chat completions with image input |
| `triage-reasoner` | `qwen38` | Chat completions |
| `eval-judge` | `compass-judger` | Chat completions |
| `embed` | `nomic-embed` | Embeddings |

Set `NATIVE_LITELLM_BASE_URL` to the native proxy's OpenAI-compatible base URL including `/v1`,
for example `http://host.docker.internal:4000/v1`, and set `NATIVE_LITELLM_API_KEY` to its existing
credential. The upstream URL must identify a different proxy from this Compose service to avoid
a forwarding loop. These native names are gateway aliases, not assumed Ollama model identifiers.

To use the native proxy directly, leave the `gateway` profile disabled and configure the future
application client with its URL and credential. The native proxy must first expose all four
application aliases above with the required endpoint types. Merely exposing `glm-ocr`, `qwen38`,
`compass-judger`, and `nomic-embed` does not satisfy that contract. No native configuration is changed
by this repository.

## Direct Ollama configuration

Set `OLLAMA_BASE_URL` to the Ollama server root, such as `http://host.docker.internal:11434`, without
`/v1`. Inspect the installed models with `ollama list`, then supply their actual identifiers:

| Variable | Required value |
| --- | --- |
| `OLLAMA_EXTRACT_MODEL` | `ollama_chat/` followed by an installed image-capable model identifier |
| `OLLAMA_TRIAGE_MODEL` | `ollama_chat/` followed by an installed chat model identifier |
| `OLLAMA_JUDGE_MODEL` | `ollama_chat/` followed by an installed chat model identifier |
| `OLLAMA_EMBED_MODEL` | `ollama/` followed by an installed embedding model identifier |

The prefix belongs in the environment value because LiteLLM resolves the entire `model` field.
Record the selected model tags and digests with evaluation results; mutable Ollama tags do not
guarantee reproducibility. This file targets a local Ollama endpoint without authentication.
Use the native gateway bridge when the upstream requires gateway authentication.
[LiteLLM recommends `ollama_chat` for chat requests](https://docs.litellm.ai/docs/providers/ollama).

## Production evaluation baseline

Set `OPENAI_BASE_URL=https://api.openai.com/v1` and provide `OPENAI_API_KEY` only to the gateway.
The initial configuration uses these explicit model identifiers:

| Alias | Model identifier | Reason for the initial choice |
| --- | --- | --- |
| `extract-vision` | `gpt-4.1-mini-2025-04-14` | General multimodal model with image input and structured outputs |
| `triage-reasoner` | `gpt-4.1-mini-2025-04-14` | Small model with structured outputs and function calling |
| `eval-judge` | `gpt-4.1-2025-04-14` | Larger model for the evaluation rubric |
| `embed` | `text-embedding-3-small` | Embeddings with an explicit 384-dimension output |

The extraction route requires image understanding. A text-only completion model or an image
generation model cannot substitute for it. The alias `triage-reasoner` describes its application
role; GPT-4.1 Mini does not expose a separate reasoning-effort setting. The dated chat snapshots
are documented by [OpenAI's GPT-4.1 Mini reference](https://developers.openai.com/api/docs/models/gpt-4.1-mini)
and [GPT-4.1 reference](https://developers.openai.com/api/docs/models/gpt-4.1). These are starting
configurations; invoice quality, cost, and latency must be measured by the later evaluation harness.

The current [GPT-6 Luna reference](https://developers.openai.com/api/docs/models/gpt-6-luna) documents
an efficient multimodal alternative, but only the undated `gpt-6-luna` identifier. This baseline
retains documented dated chat snapshots for model-version tracking instead of inventing a date.
The [embedding model reference](https://developers.openai.com/api/docs/models/text-embedding-3-small)
likewise documents only `text-embedding-3-small`; it has no dated snapshot to pin. Record the actual
response model and configuration version in evaluation provenance.

`dimensions: 384` matches the planned database vector column. OpenAI supports shortening these
embeddings through [the dimensions parameter](https://developers.openai.com/api/docs/guides/embeddings).
The native and Ollama routes must supply compatible 384-dimension vectors before near-duplicate
matching is enabled. They intentionally do not force a shortening parameter on a backend whose
support has not been verified. Different embedding models produce different vector spaces even
at the same dimension: rebuild embeddings when switching providers or models.

## Validate without inference

Compose's `config --quiet` checks the Compose document. Starting the gateway additionally checks
that LiteLLM can load the selected model configuration. All required variables must be nonempty
and supplied to the gateway container. Use synthetic credentials and unreachable upstream URLs
for a configuration-only smoke test; never use real provider credentials for an offline test.

After a configured gateway is running, these checks make no provider requests:

```bash
docker compose exec -T litellm python - <<'PY'
import json
import os
from urllib.request import Request, urlopen

with urlopen("http://127.0.0.1:4000/health/readiness", timeout=5) as response:
    assert response.status == 200

request = Request(
    "http://127.0.0.1:4000/v1/models",
    headers={"Authorization": f"Bearer {os.environ['LITELLM_MASTER_KEY']}"},
)
with urlopen(request, timeout=5) as response:
    aliases = {model["id"] for model in json.load(response)["data"]}
assert aliases == {"extract-vision", "triage-reasoner", "eval-judge", "embed"}, aliases
PY
```

The standard healthcheck uses `/health/readiness`. Do not substitute `/health`: it sends real
requests to configured models. Background model healthchecks are explicitly disabled. Router
retries are disabled so the application gateway wrapper can own its bounded infrastructure retry
policy. Live model validation belongs in the explicit evaluation workflow.
[LiteLLM healthcheck contract](https://docs.litellm.ai/docs/proxy/health).
