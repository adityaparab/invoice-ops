# InvoiceOps Agent

**An agentic, human-in-the-loop invoice processing system for Source-to-Pay — built as a production-honest, scaled-down version of what an enterprise GenAI platform team ships at a bank.**

> Portfolio Project 1 of 3 · Target roles: Citi Lead Python AI Principal Engineer / Gen AI Transformation Lead (Source-to-Pay) · Status: **Phase 7 polish in progress.** The live `golden/v1.0.1` baseline, ADK comparison, and demo are published.

The working foundation includes a health-checked FastAPI shell, Postgres/pgvector and MinIO in
Docker Compose, reversible schema migrations, append-only audit tables with a restricted API
database role, and a durable invoice LangGraph workflow. Model calls use the configured LiteLLM
URL, key, and model-name environment variables. CI checks the Python package, real database and
object-store integrations, the React console, and Compose startup. The service accepts uploads
and signed synthetic email webhooks with durable replay and duplicate rejection; deterministic
validation, matching, policy, and human decisions commit to the
[transactional ledger](docs/LEDGER.md). The console has intake, review, dashboard, run, audit,
and eval screens. Auditors can read [run traces and cross-run invoice provenance](docs/PROVENANCE_API.md).
The [pinned Voxel51 development subset](eval/datasets/README.md) contains 32 prepared synthetic
invoices and a checksummed preparation report. Its early tier-A field F1 was 0.7226. The later
500-case [live golden-set results](#7-evaluation--metrics) use a different labeled corpus and
versioned extraction pipeline; the two F1 values are not directly comparable.

---

## 1. What This Project Is

InvoiceOps Agent is an end-to-end **agentic invoice processing platform** that automates the accounts-payable intake-to-approval workflow:

1. **Ingest** invoices (email attachment, upload, or API push)
2. **Extract** structured data from unstructured documents (PDFs, scans, photos)
3. **Validate** fields (schema, math, tax, vendor master data)
4. **3-way match** invoice ↔ purchase order ↔ goods receipt against an ERP database
5. **Run policy & compliance checks** (spend limits, approval matrices, duplicate/fraud detection)
6. **Decide**: auto-approve (straight-through processing) or triage to a human with a full evidence package
7. **Record everything**: every decision, model call, tool call, and human action lands in an append-only audit ledger

It is deliberately built the way a bank would require it: **deterministic controls run before and after every LLM call**, humans stay in the loop for consequential decisions, and every output is traceable to its evidence.

### Why this project (for the target roles)

| JD requirement (from the 4 Citi postings) | How this project demonstrates it |
|---|---|
| Agentic & multi-step workflows, tool use, state management, orchestration (JD2 §2) | LangGraph state machine with durable execution, checkpointing, tool-calling agents |
| `FastAPI, ADK, and internal libraries` (JD1) | FastAPI async service; ADK variant of the same agent + comparison ADR |
| Human-in-the-loop validation, guardrails (JD2 §8) | Confidence gate with abstention → HITL exception queue; deterministic policy engine |
| Evaluation frameworks, regression validation (JD1, JD2 §1/§4/§8) | Golden dataset with injected anomalies; committed live reports are regression-gated in CI on every PR |
| Auditability, data lineage, Risk & Control partnership (JD2 §3/§8, JD3/JD4) | Append-only audit ledger; decision provenance (model + prompt + policy versions) |
| Robust error handling, observability, test coverage (JD1) | Retries, idempotency, DLQ; OpenTelemetry traces per graph node; unit + integration + eval tests |
| Financial industry / Source-to-Pay domain (JD2 role title; "financial industry is a major advantage") | The core AP workflow: 3-way match, exception handling, approval routing |

---

## 2. Problem Statement

In a large bank's Source-to-Pay organization, accounts payable teams process tens of thousands of vendor invoices per month. The dominant cost is **exception handling**: invoices that fail a match, miss a PO, exceed a limit, or look fraudulent require a human to pull documents, compare line items, chase vendors, and document the decision for auditors.

Classic OCR + RPA automated the happy path but breaks on layout variety and can't reason about *why* an invoice fails or *what to do next*. Pure LLM chatbots are unusable here: no audit trail, no determinism, no controls.

**The gap this project fills:** an agent system that combines LLM extraction and reasoning with deterministic matching/policy controls and structured human-in-the-loop escalation — measured with a real evaluation harness, not vibes.

### Goals

- **G1** — Automate intake → decision for ≥ 70% of invoices with zero policy violations (straight-through processing rate)
- **G2** — Detect ≥ 98% of injected anomalies (duplicates, mismatches, fraud patterns) at ≤ 5% false-escalation rate
- **G3** — Every automated decision reconstructible from the audit ledger in under 1 minute
- **G4** — Field-level extraction F1 ≥ 0.95 on the golden dataset
- **G5** — Cost per invoice ≤ $0.04 and p95 end-to-end latency ≤ 45s for the auto-approve path

### Non-goals (explicitly out of scope)

- Payment execution / ERP writeback (simulated with a stub)
- Vendor onboarding or sourcing (upstream S2P stages)
- Training custom OCR models (we use open OCR/VLM tools; fine-tuning is Project 4)
- Multi-tenant, SSO, real bank data — **synthetic data only**

---

## 3. Personas

| Persona | Role | What they need from the system |
|---|---|---|
| **Maria Chen** | AP Analyst (primary user) | A prioritized exception queue with evidence packages — comparisons, agent findings, suggested actions — so she can clear exceptions in minutes, not hours |
| **Dan Okafor** | Procurement Ops Manager | Dashboard: volumes, STP rate, aging exceptions, cost per invoice; confidence that nothing policy-violating auto-approves |
| **Priya Sharma** | Internal Audit / Risk | Full decision provenance: who/what decided, based on which evidence, using which model & policy versions; exportable trails |
| **Platform Engineer** (you, in the demo narrative) | Runs the platform | Traces, evals in CI, model-routing policy, cost telemetry |

---

## 4. User Journey (see the [invoice workflow](docs/INVOICE_WORKFLOW.md))

**Happy path (straight-through):** Invoice arrives by email → agent extracts fields (VLM/OCR tool) → schema + math validation passes → 3-way match succeeds → policy checks pass → confidence ≥ threshold → auto-approve → archived with full trace → queued for payment. No human touched it; a human can reconstruct why.

**Exception path (the interesting one):** Price mismatch found in 3-way match → agent gathers evidence, computes deltas, classifies exception type, drafts a recommendation → lands in Maria's queue → Maria reviews the side-by-side comparison and agent analysis → approves / returns to vendor / escalates → decision written to audit ledger with her identity → metrics update.

**Video:** [watch the 3-minute-47-second live demo](docs/demo/invoiceops-demo.mp4), with [storyboard and narration](docs/DEMO_VIDEO_SCRIPT.md).

---

## 5. System Overview (full detail in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md))

```
                 ┌──────────────────────────────────────────────────────────┐
Email / Upload / │                    INGESTION (FastAPI)                   │
API push  ─────► │  dedupe · virus-scan stub · idempotency key · raw store  │
                 └────────────────────────┬─────────────────────────────────┘
                                          ▼
                 ┌──────────────────────────────────────────────────────────┐
                 │            ORCHESTRATION — LangGraph state machine       │
                 │  (durable execution, checkpointing per node, retries)    │
                 │                                                        │
                 │  Extract ─► Validate ─► Match3Way ─► Policy ─► Gate ─►…  │
                 │      (VLM/OCR tool)  (deterministic)  (rules)  (conf)   │
                 │                               │                          │
                 │              ┌────────────────┴───────────────┐          │
                 │              ▼                                ▼          │
                 │       AutoApprove ◄── conf ≥ τ          ExceptionTriage │
                 │              │                     (agent: evidence,    │
                 │              ▼                      classification,     │
                 │           Archive                    recommendation)    │
                 └──────────────┬───────────────────────────┬──────────────┘
                                │                           ▼
                                │                  ┌──────────────────┐
                                │                  │ HITL Review Queue│◄─ Maria
                                │                  └────────┬─────────┘
                                ▼                           ▼
                 ┌──────────────────────────────────────────────────────────┐
                 │  AUDIT LEDGER (append-only) · POSTGRES · every decision, │
                 │  model call, tool call, prompt/policy version, human act │
                 └──────────────────────────────────────────────────────────┘
   Cross-cutting: gateway client over the configured LiteLLM proxy ·
                  OpenTelemetry + Langfuse tracing · Grafana dashboards
```

**Stack:** Python 3.12 · LangGraph (+ Google ADK variant) · FastAPI (async) · PostgreSQL + pgvector (ERP-sim + ledger) · LiteLLM-routed vision extraction · Pydantic v2 for schema contracts · Docker Compose · GitHub Actions (unit → integration → **eval gate**) · OpenTelemetry + Langfuse + Grafana.

**Key design decisions (ADRs live in [`adr/`](adr/)):**

1. **Determinism at the edges, intelligence in the middle.** Matching and policy are deterministic code; the LLM handles extraction, classification, and evidence summarization. This is what makes the system auditable. — [ADR 0001](adr/0001-deterministic-matcher-policy.md)
2. **Confidence gate with abstention.** Below threshold τ the system *must* escalate rather than guess — tuning τ is an eval-driven decision, documented as an experiment. — [ADR 0003](adr/0003-composite-confidence-gate.md)
3. **ADK and LangGraph variants of the same graph**, with a [measured comparison](adr/0011-langgraph-adk-comparison.md) of recovery, human review, observability, ergonomics, cloud fit, and live quality — [ADR 0002](adr/0002-langgraph-primary-adk-variant.md), [ADR 0011](adr/0011-langgraph-adk-comparison.md)
4. **Every LLM call goes through the LLM Gateway**: model routing by task class and data-sensitivity tier, public-data semantic caching, PII redaction, token budgets, cost telemetry. — [ADR 0005](adr/0005-gateway-only-model-traffic.md), [ADR 0008](adr/0008-direct-litellm-environment.md), [ADR 0010](adr/0010-public-cache-and-gateway-hardening.md) (one guarded client using the configured LiteLLM endpoint)
5. **Synthetic data only**, generated with known ground truth — itself a talking point about data governance in banking. — [ADR 0006](adr/0006-synthetic-data-anomalies.md)
6. **Append-only audit ledger** with point-in-time version pinning — [ADR 0004](adr/0004-append-only-ledger.md) · **deterministic replay in tests** via recorded LLM cassettes — [ADR 0007](adr/0007-vcr-cassettes.md)

---

## 6. Data Strategy (summary — details in [docs/EVALUATION.md](docs/EVALUATION.md))

- **Base corpus:** [Voxel51 High-Quality Invoice Images for OCR](https://huggingface.co/datasets/Voxel51/high-quality-invoice-images-for-ocr) (8,181 images, 1,489 fully annotated)
- **Synthetic ERP:** Faker-generated vendors, purchase orders, and goods receipts in Postgres, so 3-way matches have a ground-truth backend
- **Anomaly injection:** a controlled catalog of 10 anomaly types (duplicate invoice, price mismatch, quantity mismatch, missing PO, vendor-bank-detail change, currency mismatch, tax errors, line-math errors, stale/closed PO, partial-delivery mismatch) with prevalence weights mirroring real AP queues
- **Golden dataset:** 500 labeled invoices (350 clean, 150 anomalous) with a 100-case development split and a 400-case held-out split; [source and label eligibility](docs/EVALUATION.md) are published

---

## 7. Evaluation & Metrics (method in [docs/EVALUATION.md](docs/EVALUATION.md))

The `golden/v1.0.1` corpus has 500 labeled synthetic invoices. Each variant completed three
independent 500-case live Compose runs. The primary scorer uses the first complete run for
quality and proxy-reported cost, then pools audited auto-approval latencies across all three.
The [LangGraph/OpenAI production report](eval/reports/golden-v1.0.1-openai-prod.json) is the
release baseline; the [ADK/Gemini report](eval/reports/golden-v1.0.1-adk-gemini.json) is a
separate comparison. The thresholds below were not changed for the comparison.

| Primary measure | Floor | LangGraph / OpenAI routes | ADK / Gemini route |
| --- | ---: | ---: | ---: |
| Exception recall | ≥ 98% | 149/150 · 99.33% | 150/150 · 100% |
| Clean false escalation | ≤ 5% | 10/300 · 3.33% | 1/300 · 0.33% |
| Field F1 | ≥ 95% | 97.32% | 97.80% |
| Money-field F1 | ≥ 97% | 99.13% | 99.38% |
| Routing accuracy | ≥ 95% | 440/450 · 97.78% | 449/450 · 99.78% |
| Clean straight-through approval | ≥ 70% | 290/300 · 96.67% | 299/300 · 99.67% |
| LiteLLM-reported cost per invoice | ≤ $0.04 | $0.00122339726 · 500/500 covered | $0.00 reported · 500/500 covered † |
| Audited auto-approval p95 | ≤ 45 s | 30.872508 s · 875 observations | 7.713483 s · 895 observations |

All eight reported numeric values meet their floors. † LiteLLM returned zero cost for every
ADK/Gemini invoice in all three runs, so actual provider spend is unverified; the zero is a
proxy value, not a savings claim. The ADK run also changed extraction and triage model routes.
Metric differences therefore describe the whole variant and cannot isolate a framework effect.
These synthetic results do not estimate accuracy on real vendor traffic. The goal of audit
reconstruction within one minute has no measured timing result yet.

The [LangGraph diagnostics](eval/reports/diagnostics-golden-v1.0.1-openai-prod.json) and
[ADK diagnostics](eval/reports/diagnostics-golden-v1.0.1-adk-gemini.json) show anomaly confusion,
field breakdowns, and threshold sweeps. [ADR 0011](adr/0011-langgraph-adk-comparison.md)
documents the comparison and limits; the [experiment log](eval/reports/experiment-log-v1.json)
records the decisions. CI compares
committed complete `openai-prod` reports against the main-branch release baseline and enforces
the existing floors and regression tolerance. Live model evaluations run separately; CI does
not call the provider on each PR.

---

## 8. Governance & Controls Mapping

| Control (engineering) | Regulatory anchor |
|---|---|
| Append-only audit ledger, decision provenance (model, prompt, policy versions) | OCC Bulletin 2026-13 (successor to SR 11-7) documentation & validation expectations; EU AI Act Art. 12 record-keeping |
| Human-in-the-loop for consequential decisions, confidence abstention | EU AI Act human-oversight requirements for high-risk systems |
| Eval suite + regression gates in CI | Model validation / ongoing monitoring expectations |
| PII redaction + data-sensitivity routing via LLM Gateway | Data-minimization and privacy-by-design principles |
| Deterministic policy checks independent of the LLM | Model-risk principle: controls not solely dependent on the model being validated |

---

## 9. Repository Layout

| Path | Responsibility |
| --- | --- |
| [`src/invoiceops_agent/`](src/invoiceops_agent/) | API, LangGraph/ADK orchestration, agents, deterministic tools, ledger, gateway client, and observability |
| [`frontend/`](frontend/) | React operations console and browser smoke |
| [`eval/golden/`](eval/golden/) and [`eval/reports/`](eval/reports/) | Versioned labels, live primary/diagnostic JSON reports, and experiment log |
| [`tests/`](tests/) | Offline unit and Docker-backed integration tests |
| [`compose.yaml`](compose.yaml) and [`deploy/`](deploy/) | Local stack, images, database setup, and observability configuration |
| [`docs/`](docs/) and [`adr/`](adr/) | Contracts, evaluation method, demo, and architecture decisions |
| [`.github/workflows/ci.yml`](.github/workflows/ci.yml) | Lint, strict types, tests, builds, Compose/browser smoke, and report gate |

---

## 10. Implementation Status

Phases 0–6 are complete. Phase 7 has a [recorded demo](docs/demo/invoiceops-demo.mp4)
and the measured results above; the experiment-log narrative is the remaining polish step.
The checked steps, dates, and progress notes live in [`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md).

---

## 11. Risks & Mitigations

| Risk | Mitigation |
|---|---|
| VLM extraction accuracy on poor scans | Preprocessing pipeline + confidence gating escalates rather than guessing; report accuracy by document-quality tier |
| Synthetic data → "too clean" results | Inject layout noise, rotations, stamps, handwriting-style artifacts; publish per-tier metrics |
| Agent nondeterminism flaky tests | Deterministic replay in tests via recorded LLM responses (VCR-style); eval suite uses fixed seeds + N-run averaging |
| Scope creep toward full ERP simulation | Non-goals enforced; payment is a stub; PR template includes scope check |

---

## 12. Documents in This Folder

| File | Purpose |
|---|---|
| [`README.md`](README.md) | This master project description |
| [`docs/INVOICE_WORKFLOW.md`](docs/INVOICE_WORKFLOW.md) | Invoice graph route and review behavior |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Components, LangGraph state machine, API design, data model, observability |
| [`docs/EVALUATION.md`](docs/EVALUATION.md) | Golden dataset design, anomaly catalog, metrics, CI eval harness |
| [`docs/DEMO_VIDEO_SCRIPT.md`](docs/DEMO_VIDEO_SCRIPT.md) | Storyboard and narration for the demo video |
| [`docs/demo/invoiceops-demo.mp4`](docs/demo/invoiceops-demo.mp4) | 3-minute-47-second live browser recording |
| [`eval/reports/golden-v1.0.1-openai-prod.json`](eval/reports/golden-v1.0.1-openai-prod.json) | Complete production-route live baseline |

---

## 13. Development Quickstart

```bash
uv sync --locked                          # install env from uv.lock
uv run ruff check .                       # lint
uv run ruff format --check .              # formatting
uv run mypy                               # strict type check (src, tests, deploy, migrations)
uv run pytest -m unit --disable-socket --allow-unix-socket  # offline unit tests
uv build                                 # build the wheel and source distribution
```

Python 3.12 is selected by `.python-version`. Development dependencies have exact versions in `pyproject.toml`, with
transitive dependencies recorded in `uv.lock`; use `uv sync --locked` to verify the lockfile without
updating it. `uv` installs a compatible Python 3.12 interpreter automatically when needed.

The installed namespace is `invoiceops_agent`; its component packages follow the architecture
boundaries in `AGENTS.md`. The API shell exposes `/healthz`, dependency readiness at `/readyz`,
RFC 7807 errors, and validated request context. Run it with
`uv run uvicorn invoiceops_agent.api.app:create_app --factory`; see [API shell setup](docs/API_SHELL.md)
for configuration and contracts. Package smoke tests verify the installed entry points and typing marker.
Pytest uses strict configuration and markers, with function-scoped asyncio loops. Future tests
must declare `unit`, `integration`, or `eval` as appropriate; async tests use `@pytest.mark.asyncio`.

Build the pinned MinIO image with `bash scripts/prepare_minio_image.sh`, then
start the local API, Postgres, and MinIO with `docker compose up -d --build --wait`.
See [local platform setup](deploy/README.md) for credentials, persistent volumes, seed operation,
and the optional observability profile. Run `docker compose run --rm graph-demo` for the durable
LangGraph hello path; [graph demo instructions](docs/GRAPH_HELLO.md) cover replay and resume. See
[invoice ingestion instructions](docs/INGESTION.md) for multipart uploads, the signed webhook,
limits, and durable replay. Accepted invoices queue for the invoice worker; the
[pipeline runner](eval/runners/README.md) exercises API intake, worker processing, and audit
readback through the real stack.
GitHub Actions runs linting, formatting, strict type checking, offline unit tests, real pgvector and
MinIO tests through disposable Testcontainers, a package build, and Compose/browser smoke on
every PR and push to `main`. Pull requests also run the committed-report golden gate.
To run the infrastructure tests locally, prepare the MinIO image, start Docker,
and run `uv run pytest -m integration`.
The CI report gate uses committed live evaluation evidence and makes no live model calls itself.

Database migrations run with a separate owner connection in `INVOICEOPS_MIGRATION_DSN`:
`uv run alembic upgrade head`. The Compose `migrate` service additionally provisions the restricted
`invoiceops_app` login from `INVOICEOPS_APP_PASSWORD` before the API starts. The API receives only
runtime credentials. Audit tables reject updates, deletes, and truncation, including owner writes;
corrections append a superseding entry. See [schema and migration commands](docs/schema.md) for
table contracts, credential provisioning, and reversible migration behavior.

The [gateway client](docs/GATEWAY_CLIENT.md) provides typed async chat and embedding calls through
LiteLLM task aliases, with text guards, schema validation, token allowances, bounded retries, and offline
cassette replay. Configure its endpoint, key, and model names through `LITELLM_*` variables; binary inputs
require a compatible route and trusted document preprocessing.

Layout, quality bar, and workflow rules for agents and contributors live in [`AGENTS.md`](AGENTS.md); the build tracker is [`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md).

The [extraction agent](docs/EXTRACTION.md) provides bounded document preparation, typed
per-field observations, one technical schema-repair pass, and committed audit outcomes through
the gateway. The worker invokes it inside the durable invoice graph; the complete live
golden-set measurements are linked above.
