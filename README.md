# InvoiceOps Agent

**An agentic, human-in-the-loop invoice processing system for Source-to-Pay — built as a production-honest, scaled-down version of what an enterprise GenAI platform team ships at a bank.**

> Portfolio Project 1 of 3 · Target roles: Citi Lead Python AI Principal Engineer / Gen AI Transformation Lead (Source-to-Pay) · Status: **Building** — Phase 0 package scaffold and development tooling; application features below describe the target system.

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
| Evaluation frameworks, regression validation (JD1, JD2 §1/§4/§8) | Golden dataset with injected anomalies; eval suite runs in CI on every PR |
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

## 4. User Journey (summary — full detail in [docs/USER_JOURNEY.md](docs/USER_JOURNEY.md))

**Happy path (straight-through):** Invoice arrives by email → agent extracts fields (VLM/OCR tool) → schema + math validation passes → 3-way match succeeds → policy checks pass → confidence ≥ threshold → auto-approve → archived with full trace → queued for payment. No human touched it; a human can reconstruct why.

**Exception path (the interesting one):** Price mismatch found in 3-way match → agent gathers evidence, computes deltas, classifies exception type, drafts a recommendation → lands in Maria's queue → Maria reviews the side-by-side comparison and agent analysis → approves / returns to vendor / escalates → decision written to audit ledger with her identity → metrics update.

**Working mock:** open **[`mocks/index.html`](mocks/index.html)** in any browser (self-contained, no server or internet needed). It walks through all screens — Dashboard → Intake → animated Agent Run → Exception Review with 3-way match → Audit/Trace → Evals — and both paths (auto-approve and exception) are clickable.

**Video:** a 3–4 minute recorded demo following [docs/DEMO_VIDEO_SCRIPT.md](docs/DEMO_VIDEO_SCRIPT.md) (script written; recording happens when the real system is live).

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
   Cross-cutting: LLM Gateway (Project 3) for routing/caching/guardrails ·
                  OpenTelemetry + Langfuse tracing · Grafana dashboards
```

**Stack:** Python 3.12 · LangGraph (+ Google ADK variant) · FastAPI (async) · PostgreSQL + pgvector (ERP-sim + ledger) · Surya/Marker or Qwen-VL for document extraction · Pydantic v2 for schema contracts · Docker Compose · GitHub Actions (unit → integration → **eval gate**) · OpenTelemetry + Langfuse + Grafana.

**Key design decisions (ADRs live in [`adr/`](adr/)):**

1. **Determinism at the edges, intelligence in the middle.** Matching and policy are deterministic code; the LLM handles extraction, classification, and evidence summarization. This is what makes the system auditable. — [ADR 0001](adr/0001-deterministic-matcher-policy.md)
2. **Confidence gate with abstention.** Below threshold τ the system *must* escalate rather than guess — tuning τ is an eval-driven decision, documented as an experiment. — [ADR 0003](adr/0003-composite-confidence-gate.md)
3. **ADK and LangGraph variants of the same graph**, with an ADR comparing developer ergonomics, checkpointing, observability, and cloud fit — demonstrating framework judgment, not framework loyalty. — [ADR 0002](adr/0002-langgraph-primary-adk-variant.md)
4. **Every LLM call goes through the LLM Gateway**: model routing by task class and data-sensitivity tier, semantic caching, PII redaction, token budgets, cost telemetry. — [ADR 0005](adr/0005-gateway-only-model-traffic.md) (implemented as a LiteLLM proxy with virtual model aliases)
5. **Synthetic data only**, generated with known ground truth — itself a talking point about data governance in banking. — [ADR 0006](adr/0006-synthetic-data-anomalies.md)
6. **Append-only audit ledger** with point-in-time version pinning — [ADR 0004](adr/0004-append-only-ledger.md) · **deterministic replay in tests** via recorded LLM cassettes — [ADR 0007](adr/0007-vcr-cassettes.md)

---

## 6. Data Strategy (summary — details in [docs/EVALUATION.md](docs/EVALUATION.md))

- **Base corpus:** [Voxel51 High-Quality Invoice Images for OCR](https://huggingface.co/datasets/Voxel51/high-quality-invoice-images-for-ocr) (8,181 images, 1,489 fully annotated)
- **Synthetic ERP:** Faker-generated vendors, purchase orders, and goods receipts in Postgres, so 3-way matches have a ground-truth backend
- **Anomaly injection:** a controlled catalog of 10 anomaly types (duplicate invoice, price mismatch, quantity mismatch, missing PO, vendor-bank-detail change, currency mismatch, tax errors, line-math errors, stale/closed PO, partial-delivery mismatch) with prevalence weights mirroring real AP queues
- **Golden dataset:** 500 labeled invoices (≈350 clean, ≈150 anomalous) held out for eval; test/train discipline enforced in the harness

---

## 7. Evaluation & Metrics (summary — details in [docs/EVALUATION.md](docs/EVALUATION.md))

| Metric | Target | Definition |
|---|---|---|
| Straight-through processing (STP) rate | ≥ 70% | % of invoices auto-approved with zero human touch |
| Exception detection F1 | ≥ 0.98 recall, ≤ 0.05 false-escalation | Against injected anomalies, per type + macro |
| Extraction field F1 | ≥ 0.95 | Per-field exact match over golden labels (vendor, IBAN, amounts, dates, line items) |
| Cost per invoice | ≤ $0.04 | Tokens × unit price, averaged over the golden set |
| p95 latency | ≤ 45s | Auto-approve path, end-to-end |
| Audit reconstruction time | ≤ 1 min | From invoice ID → complete decision provenance |

The eval suite is a **CI gate**: pull requests fail if any metric regresses beyond tolerance. Version-over-version results are published as a report (the "experiment log") — the flagship written artifact of this project.

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

## 9. Repository Layout (planned)

```
InvoiceOps/
├── README.md                  # this document
├── docs/
│   ├── USER_JOURNEY.md        # personas + step-by-step journeys
│   ├── ARCHITECTURE.md        # components, state machine, APIs, data model
│   ├── EVALUATION.md          # golden dataset, metrics, CI harness
│   └── DEMO_VIDEO_SCRIPT.md   # 3–4 min video storyboard + narration
├── mocks/
│   └── index.html             # interactive working mock (open in browser)
├── src/invoiceops_agent/      # installable Python package; component boundaries below
│   ├── api/                   # FastAPI app, routes, dependencies
│   ├── graph/                 # LangGraph definition + ADK variant
│   │   ├── nodes/             # extract, validate, match, policy, gate, triage
│   │   └── state.py           # typed graph state (Pydantic)
│   ├── agents/                # extraction agent, triage agent (tool-using)
│   ├── tools/                 # OCR/VLM tool, ERP repo, policy engine, dedupe
│   ├── ledger/                # append-only audit ledger writer/reader
│   ├── gateway_client/        # LLM Gateway SDK (Project 3)
│   └── obs/                   # OTel setup, trace exporters
├── eval/
│   ├── golden/                # labeled dataset + anomaly catalog
│   ├── runners/               # metric implementations
│   └── reports/               # versioned HTML/MD eval reports
├── tests/                     # unit + integration (testcontainers)
├── deploy/                    # Docker Compose, Grafana dashboards, seed data
├── adr/                       # Architecture Decision Records
└── .github/workflows/ci.yml   # lint → test → eval gate → build
```

---

## 10. Build Plan (from here)

| Phase | Duration | Exit criteria |
|---|---|---|
| **P0 — Platform skeleton** | Week 1 | FastAPI + Postgres + LangGraph hello-path in Docker Compose; CI green |
| **P1 — Extraction & validation** | Weeks 2–3 | Voxel51 subset processed; extraction field F1 measured; ledger records every step |
| **P2 — 3-way match + policy** | Week 4 | Synthetic ERP; deterministic checks; exception taxonomy implemented |
| **P3 — HITL + triage agent** | Week 5 | Review queue UI; decisions written to ledger; confidence gate tuned via eval |
| **P4 — Observability + gateway integration** | Week 6 | OTel traces per node; cost/latency dashboards; routed through LLM Gateway |
| **P5 — Eval harness + report** | Weeks 7–8 | 500-invoice golden set; CI eval gate; published v0.1→v0.3 experiment report |
| **P6 — ADK variant + ADR** | Week 9 | Same graph in ADK; comparison ADR written |
| **P7 — Polish** | Week 10 | Recorded demo video; README with metrics tables; blog-post draft |

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
| [`docs/USER_JOURNEY.md`](docs/USER_JOURNEY.md) | Personas and step-by-step user journeys (all paths) |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Components, LangGraph state machine, API design, data model, observability |
| [`docs/EVALUATION.md`](docs/EVALUATION.md) | Golden dataset design, anomaly catalog, metrics, CI eval harness |
| [`docs/DEMO_VIDEO_SCRIPT.md`](docs/DEMO_VIDEO_SCRIPT.md) | Storyboard and narration for the demo video |
| [`mocks/index.html`](mocks/index.html) | **Interactive working mock** — the user journey, clickable, in one self-contained file |

---

## 13. Development Quickstart

```bash
uv sync                                   # install env from uv.lock
uv run ruff check .                       # lint
uv run ruff format --check .              # formatting
uv run mypy                               # strict type check (src + tests)
uv run pytest -m unit                     # offline package smoke tests
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

Compose, the graph demo, and invoice ingestion are planned work in steps 0.3, 0.8, and 1.1
respectively. Their services and example fixtures are not available yet.
GitHub Actions runs linting, formatting, strict type checking, offline unit tests, real pgvector and
MinIO tests through disposable Testcontainers, and a package build on every PR and push to `main`.
To run the infrastructure tests locally, start Docker and run `uv run pytest -m integration`.
The model evaluation gate is planned in Phase 5; these tests make no live model calls.

Database migrations run with a separate owner connection in `INVOICEOPS_MIGRATION_DSN`:
`uv run alembic upgrade head`. See [schema and migration commands](docs/schema.md) for the URL format,
table contracts, and reversible migration behavior. Runtime-role grants and audit immutability are
the following step, 0.7.

Layout, quality bar, and workflow rules for agents and contributors live in [`AGENTS.md`](AGENTS.md); the build tracker is [`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md).
