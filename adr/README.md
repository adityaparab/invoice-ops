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

The cross-cutting decision summaries are linked from `README.md` §5 and
`docs/ARCHITECTURE.md` §4.
