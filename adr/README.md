# Architecture Decision Records

ADRs record *why* a choice was made; `docs/ARCHITECTURE.md` records *what* the system is.
Amending a decision produces a new ADR or a status change here — never a silent rewrite.

New records start from [`template.md`](template.md): Status · Date · Deciders · Context ·
Decision · Consequences.

| ADR | Decision | Status |
|---|---|---|
| [0001](0001-deterministic-matcher-policy.md) | Deterministic matcher/policy instead of LLM-judged matching | Accepted |
| [0002](0002-langgraph-primary-adk-variant.md) | LangGraph primary, ADK comparison variant | Accepted |
| [0003](0003-composite-confidence-gate.md) | Composite confidence gate; abstention over guessing | Accepted |
| [0004](0004-append-only-ledger.md) | Append-only ledger with point-in-time version pinning | Accepted |
| [0005](0005-gateway-only-model-traffic.md) | Gateway-only model traffic (implemented via LiteLLM proxy) | Accepted |
| [0006](0006-synthetic-data-anomalies.md) | Synthetic data with injected anomalies; published prevalences | Accepted |
| [0007](0007-vcr-cassettes.md) | VCR-style recorded LLM responses in tests | Accepted |
| [0008](0008-direct-litellm-environment.md) | Operator-provided LiteLLM URL, key, and model names | Accepted |
| [0009](0009-gateway-boundary-llm-tracing.md) | Trace model calls at the application gateway | Accepted |
| [0010](0010-public-cache-and-gateway-hardening.md) | Explicit public caching and gateway hardening | Accepted |
| [0011](0011-langgraph-adk-comparison.md) | Implemented LangGraph and ADK workflow comparison | Accepted |

The cross-cutting decision summaries are linked from `README.md` §5 and
`docs/ARCHITECTURE.md` §4.

Verified for implementation step 0.10 on 2026-09-23: all seven accepted records were already present
in the repository's initial commit. Their original decision dates and statuses are preserved. Package
paths now refer to the installed `invoiceops_agent` namespace, and tracker references use plan steps
until GitHub issues exist. ADR 0011 now records the Phase 6 implementation comparison.
