# Implementation Plan & Progress Tracker — InvoiceOps Agent

> **Purpose:** Single source of truth for building the system described in `README.md`, `docs/ARCHITECTURE.md`, and `docs/EVALUATION.md`. Work through phases top-to-bottom; check off steps as they complete. Update the status tables at the bottom as phases finish.
>
> **Last updated:** 2026-09-24 (Phase 5 in progress)

---

## Technology Stack (locked)


| Layer         | Choice                                                                                                                                                                                            | Notes                                                                                                                                         |
| ------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------- |
| Python        | **3.12**                                                                                                                                                                                          | `.python-version`, package metadata, Ruff, and mypy aligned to Python 3.12                                                                   |
| Orchestration | **LangGraph** + Postgres checkpointer                                                                                                                                                             | Google ADK variant in Phase 6 (ADR 0002)                                                                                                      |
| API           | **FastAPI** (async, uvicorn), Pydantic v2                                                                                                                                                         | RFC 7807 errors, idempotency-key header                                                                                                       |
| DB            | **PostgreSQL + pgvector**, raw docs in **MinIO**                                                                                                                                                  | ERP sim + ledger + checkpoints                                                                                                                |
| LLM access    | **Operator LiteLLM endpoint** (OpenAI-compliant API) for all model traffic | One guarded client uses `LITELLM_API_BASE`, `LITELLM_MASTER_KEY`, and task model-name variables; internal aliases select task policy |
| LLM backends  | **Dev/prod/eval:** routes exposed by the configured LiteLLM deployment | Switching = direct environment model-name change; no local proxy YAML |
| Extraction    | Dev: local vision model (Ollama-backed) · Prod: hosted vision LLM (e.g. GPT-4o class)                                                                                                             | Routed via LiteLLM alias `extract-vision`                                                                                                     |
| Observability | **OpenTelemetry** (span per graph node), **Langfuse** (LLM traces), **Prometheus + Grafana**                                                                                                      | Cost telemetry reads LiteLLM spend logs too                                                                                                   |
| Testing       | pytest, hypothesis, testcontainers, schemathesis, VCR-style cassettes                                                                                                                             | ADR 0007                                                                                                                                      |
| CI/CD         | GitHub Actions: ruff → mypy → unit → integration → **eval gate** → build                                                                                                                          | Gate: fail PR on >0.5% absolute regression or metric-floor breach                                                                             |
| Front end     | **React + TypeScript (Vite)** · **Mantine** UI library                                                                                                                                            | See "Front-End Stack" below                                                                                                                   |
| Deployment    | Docker Compose (api, worker, postgres, minio, optional langfuse, grafana+prometheus, seed) | Cloud variant documented as notes only |


**Updated (2026-09-24):** the application uses only the operator-provided LiteLLM URL, API key, and model-name variables. ADR 0008 supersedes the former local proxy configuration.

### Front-End Stack (locked)


| Concern            | Choice                                                                                                       |
| ------------------ | ------------------------------------------------------------------------------------------------------------ |
| Framework / build  | React + TypeScript, **Vite**                                                                                 |
| UI components      | **Mantine** (v7+) with `@mantine/hooks`, `@mantine/notifications`, `@mantine/dates`                          |
| Server state       | **TanStack Query**                                                                                           |
| Tables             | **TanStack Table** (exception queue, invoice list) with Mantine styling                                      |
| Forms & validation | **react-hook-form** + **Zod** (Zod schemas mirror the Pydantic API contracts)                                |
| Charts             | **Recharts** (STP rate, cost per invoice, latency, aging)                                                    |
| Routing            | **React Router**                                                                                             |
| API client         | `fetch`/`openapi-fetch` client generated from FastAPI's OpenAPI schema                                       |
| Auth UX            | Persona switcher (Maria / Dan / Priya / Platform Eng) driving RBAC views; service-token auth against the API |


**Screens (from** `mocks/index.html` **+ docs/USER_JOURNEY.md):** Dashboard (Dan), Intake, Agent Run (live graph progress), Exception Review with 3-way match comparison (Maria), Audit/Trace & Provenance (Priya), Evals view (experiment log).

---



## Phase 0 — Repo & Platform Skeleton (Week 1)

**Exit criteria:** FastAPI + Postgres + LangGraph hello-path in Docker Compose; CI green.

- [x] 0.1 Restructure to `src/` layout per README §9; remove `hello.py`; pin dev tooling (ruff, mypy strict, pytest, pytest-asyncio) as uv dev-dependencies
  - Merged in PR #1; no `hello.py` existed.
- [x] 0.2 Bump Python to 3.12 in `.python-version` and `pyproject.toml`
- [x] 0.3 Docker Compose stack: `api`, `postgres` (pgvector), `minio`, `litellm`; one-shot `seed` service placeholder; `langfuse` + `grafana`/`prometheus` deferred to Phase 4
- [x] 0.4 Historical local proxy route maps; superseded in step 4.1 by direct LiteLLM environment configuration (ADR 0008)
- [x] 0.5 FastAPI app shell: `/healthz`, `/readyz`, RFC 7807 error handler, Pydantic v2 settings, idempotency-key middleware
  - Implemented before 0.3 so Compose can run a real health-checked API. Idempotency context validation is ready; durable replay accompanies future mutation transactions.
- [x] 0.6 Alembic migrations for full schema (ARCHITECTURE §6): `vendors`, `purchase_orders`, `goods_receipts`, `invoices` (unique `content_hash`), `invoice_lines`, `runs`, `checkpoints`, `ledger`, `exceptions`, `decisions`
- [x] 0.7 Append-only enforcement on `ledger` + `decisions` (grants + triggers)
- [x] 0.8 LangGraph hello-path graph (stub nodes) with Postgres checkpointer, run end-to-end in Compose
- [x] 0.9 GitHub Actions CI: ruff → mypy → pytest, running against Compose (or testcontainers)
  - Brought forward so subsequent PRs can meet the passing-CI merge rule. Real pgvector and MinIO testcontainers run after offline unit tests; the model eval gate remains Phase 5 work.
- [x] 0.10 Write ADRs 0001–0007 into `adr/` (decisions already made in docs; record them)
  - All seven accepted ADRs were present in the initial repository; verified their decisions and corrected package paths and tracker references.



## Phase 1 — Ingestion, Extraction, Validation (Weeks 2–3)

**Exit criteria:** Voxel51 subset processed; extraction field F1 measured (baseline); ledger records every step.

- [x] 1.1 `POST /v1/invoices` upload endpoint (service token auth), raw doc stored in MinIO
  - Bounded multipart parsing, signature checks, content-addressed raw storage, atomic invoice/run/ledger/replay writes. Step 1.3 implements original-ID `200` responses and audited Reject routing for new-key content duplicates.
- [x] 1.2 `POST /v1/invoices/email-webhook` with HMAC verification (stub email source)
  - Bounded signed JSON, canonical base64 document handling, transactional nonce claims, shared cross-source replay, and audited duplicate routing; no provider integration.
- [x] 1.3 Content-hash dedupe on ingest → route to `Reject`
  - New keys return `200` with original IDs and `duplicate=true`; one SYSTEM Reject event and successful replay response commit atomically. Same-key replay preserves status/body without another event.
- [x] 1.4 Ledger writer/reader: append entries with actor_type (SYSTEM/AGENT/HUMAN/POLICY) and model/prompt/policy version pins
  - Brought forward so invoice ingestion can commit its initial audit entry with business data and its idempotency response.
- [x] 1.5 Gateway client: thin `openai`-SDK wrapper over LiteLLM endpoint — virtual aliases, PII redaction, schema validation, token budgets, retries/backoff
- [x] 1.6 Extraction agent: doc → typed `InvoiceExtraction` (Pydantic) with per-field confidence, via `extract-vision` alias
- [x] 1.7 Validate node: schema checks, line-math, tax checks (deterministic)
- [x] 1.8 Download + preprocess Voxel51 subset (incl. quality-tier labeling A/B/C)
- [x] 1.9 Baseline extraction field F1 report (per-field, per-tier) — no targets yet
  - The 32-image development baseline has tier A micro F1 0.7226 over seven annotated fields; tier B/C are unavailable. One model output escalated, and line-total F1 is 0.0560. No quality gate was added.



## Phase 2 — 3-Way Match + Policy Engine (Week 4)

**Exit criteria:** synthetic ERP live; deterministic checks; exception taxonomy implemented.

- [x] 2.1 Synthetic ERP generator (Faker, seed-pinned): vendors, POs, goods receipts with ground truth; seeds via Compose `seed` service
- [x] 2.2 Deterministic 3-way matcher with tolerance bands; deltas computed for evidence packages
- [x] 2.3 Exception taxonomy: DUP_EXACT, DUP_NEAR, PRICE_MM, QTY_MM, MISSING_PO, BANK_CHANGE, CCY_MM, TAX_ERR, MATH_ERR, STALE_PO
- [x] 2.4 Near-duplicate detection via pgvector embeddings
- [x] 2.5 Policy engine: spend limits, approval matrix, stale/closed-PO checks — deterministic, independent of LLM (ADR 0001)
- [x] 2.6 Full LangGraph state machine wiring: Ingest → Extract → Validate → Match3Way → Policy → Gate → (AutoApprove | ExceptionTriage) → HumanReview → Archive (+ Reject), checkpoint after every node
- [x] 2.7 Composite confidence gate: `w1·min(field_conf) + w2·(1−norm_match_delta) + w3·policy_severity_term` (ARCHITECTURE §3.5); τ configurable
- [x] 2.8 Retries/backoff for infra errors; business failures never retried; DLQ design implemented



## Phase 3 — HITL + Triage Agent + Full Front End (Weeks 5–6)

**Exit criteria:** full-fledged React UI for all six screens; decisions in ledger; confidence gate tuned via eval.

- [x] 3.1 `GET /v1/invoices` queue listing with filters; `GET /v1/invoices/{id}` aggregate view (RBAC)
- [x] 3.2 `POST /v1/exceptions/{id}/decision` with four-eyes check
- [x] 3.3 Triage agent (via `triage-reasoner` alias): evidence gathering, exception classification, recommendation draft
- [x] 3.4 Front-end scaffold: Vite + React + TS app in `frontend/` with Mantine provider, TanStack Query, React Router, generated API client from FastAPI OpenAPI schema, persona switcher (RBAC), wired to Compose (`ui` service, dev proxy to API)
- [x] 3.5 **Screen — Exception Review (Maria):** queue (TanStack Table: filter/sort/priority/SLA aging), detail view with side-by-side 3-way match comparison (invoice ↔ PO ↔ GR), extracted-fields panel with per-field confidence, agent findings & recommendation, decision form (approve / return to vendor / escalate) with rationale + reason code and four-eyes flow via react-hook-form + Zod
- [x] 3.6 **Screen — Dashboard (Dan):** STP rate, volumes, aging exceptions, cost per invoice, exception-type breakdown (Recharts); drill-through to queue
- [x] 3.7 **Screen — Intake:** invoice upload with progress, ingestion status, duplicate/reject feedback
- [x] 3.8 **Screen — Agent Run:** live LangGraph progress per node (polling or SSE), state inspection
- [x] 3.9 **Screen — Audit/Trace & Provenance (Priya):** run trace (Mantine Timeline), full ledger view with actor/version pins, provenance export
- [x] 3.10 **Screen — Evals:** experiment-log view — versioned metric tables, per-anomaly confusion, τ sweep chart; reads `eval/reports/`
- [x] 3.11 Front-end testing: Vitest + React Testing Library on decision form and queue; Playwright smoke of the happy path in CI
- [x] 3.12 Provenance endpoints: `GET /v1/runs/{run_id}/trace`, `GET /v1/invoices/{id}/provenance`



## Phase 4 — Observability + Gateway Hardening (Week 6)

**Exit criteria:** OTel traces per node; cost/latency dashboards; all traffic through LiteLLM.

- [x] 4.1 Add `langfuse`, `prometheus`, `grafana` to Compose; Grafana dashboards provisioned
- [x] 4.2 OTel spans per graph node + per tool call; exporters wired
- [x] 4.3 Langfuse tracing for all LLM calls (gateway callback to OTLP; proxy callbacks superseded by ADR 0009)
- [x] 4.4 Cost/latency dashboards: LiteLLM spend logs + OTel metrics; `GET /v1/metrics` Prometheus endpoint
- [x] 4.5 Gateway hardening: model routing by task class + data-sensitivity tier, semantic cache, fallback chain, budget alerts



## Phase 5 — Eval Harness + CI Gate (Weeks 7–8)

**Exit criteria:** 500-invoice golden set; CI eval gate live; v0.1→v0.3 experiment report published.

- [x] 5.1 Golden dataset builder: 500 invoices = 350 clean (Voxel51 re-labeled + synthetic; 30 hard negatives with rotation/skew/stamps/faint print) + 150 anomalous (10 seeded codes, weighted prevalences); versioned (`golden/v1.0.0`), seed-pinned, held-out split
- [x] 5.2 `eval/runners/run_pipeline.py` — drives the real Compose stack through the API (not mocks); `--recorded` cassette mode for smoke
- [x] 5.3 `metrics.py`: exception recall (≥0.98), false-escalation (≤0.05), field F1 (≥0.95; money fields ≥0.97), routing accuracy (≥0.95), STP (≥0.70), cost (≤$0.04/inv), p95 latency (≤45s, N=3 runs)
- [x] 5.4 Diagnostics: per-anomaly confusion, per-field/per-tier F1, calibration curve, τ sweep ROC-style curve, LLM-judge triage rubric (judge via gateway, versioned)
- [ ] 5.5 Report per model class (local-dev vs OpenAI-prod) — one extra tag through the harness
- [ ] 5.6 `ci_gate.py`: fail PR on any primary metric regressing >0.5% absolute vs main or below floor; PR comment with deltas
- [ ] 5.7 Versioned reports committed to `eval/reports/`; start the experiment log (hypothesis/change/delta/decision)



## Phase 6 — ADK Variant + Comparison ADR (Week 9)

**Exit criteria:** same graph in ADK; comparison ADR written.

- [ ] 6.1 Port the state machine to Google ADK (Gemini as the ADK-side model)
- [ ] 6.2 Comparison ADR: checkpointing, durable execution, HITL support, observability, developer ergonomics, cloud fit
- [ ] 6.3 Run the eval suite against the ADK variant; include results in the ADR



## Phase 7 — Polish (Week 10)

**Exit criteria:** recorded demo; README with metrics; blog draft.

- [ ] 7.1 Record 3–4 min demo video following `docs/DEMO_VIDEO_SCRIPT.md`
- [ ] 7.2 README: replace planned-metrics tables with measured results
- [ ] 7.3 Blog-post draft (the "experiment log" narrative)

---



## Progress Log



### GitHub issue tracker (step → issue)

The GitHub issue tracker in `adityaparab/invoice-ops` is currently empty. Reference plan step numbers
in PRs until issue mappings exist. When a step's PR merges, close its issue if present and tick the
checkbox here.


| Step | Issue | Step | Issue | Step    | Issue   |
| ---- | ----- | ---- | ----- | ------- | ------- |



| Phase                          | Status      | Completed on | Notes                                                     |
| ------------------------------ | ----------- | ------------ | --------------------------------------------------------- |
| P0 — Platform skeleton         | Complete    | 2026-09-23   | Compose API, storage, restricted runtime, audit enforcement, durable hello graph, CI, and ADRs implemented |
| P1 — Extraction & validation   | Complete    | 2026-09-23   | Ingestion, extraction, validation, and measured 32-image development baseline |
| P2 — Match + policy            | Complete    | 2026-09-24   | Deterministic matching, taxonomy, similarity, policy, durable graph, composite gate, and audited retry/DLQ |
| P3 — HITL + triage + front end | Complete    | 2026-09-24   | All Phase 3 implementation steps merged; measured tuning follows the Phase 5 golden set |
| P4 — Observability + gateway   | Complete    | 2026-09-24   | Workflow and LLM traces, cost/latency dashboards, sensitivity routing, public cache, fallback, and budget alerts |
| P5 — Eval harness + CI gate    | In progress | —            | Golden dataset, Compose runner, primary metrics, and diagnostics implemented; model-class reports, CI gate, and experiment report remain |
| P6 — ADK variant + ADR         | Not started | —            |                                                           |
| P7 — Polish                    | Not started | —            |                                                           |




## Change Log

| Date | Change |
| --- | --- |
| 2026-09-23 | Step 0.1: package scaffold and pinned tooling prepared; local checks and isolated wheel smoke tests pass. PR merge remains pending CI. |
| 2026-09-23 | PR #1 merged. Step 0.9 brought forward: GitHub Actions runs lint, strict typing, offline unit tests, isolated pgvector/MinIO integration tests, and package builds. |
| 2026-09-23 | Step 0.2 pins the developer interpreter to Python 3.12. PR #3 establishes CI before subsequent PR merges. |
| 2026-09-23 | Step 0.5 adds the async API shell, bounded dependency probes, typed HTTP/Zod contracts, sanitized error responses, trace logs, and mutation-key validation. |
| 2026-09-23 | Step 0.3 adds the pinned Compose stack, non-root API image, persistent storage volumes, optional local proxy, and explicit seed placeholder; CI smoke-tests the stack. |
| 2026-09-23 | Step 0.6 adds reversible owner-driven Alembic migrations, twelve constrained tables, Decimal-safe values, audit version pins, and the 384-dimensional vector index. |
| 2026-09-23 | Step 0.4 configures four virtual aliases for native, Ollama, and OpenAI backends; a selected-config preflight rejects missing credentials before proxy startup. All routes were startup-tested without provider access. |
| 2026-09-23 | Step 0.7 enforces append-only audit statements, provisions a restricted SCRAM runtime login, and runs owner migrations in a separate one-shot Compose service; role-isolation regressions and authenticated API checks pass. |
| 2026-09-23 | Step 0.8 adds a typed hello-stub graph, isolated Postgres checkpoints, restart/resume, completed-run replay, concurrency controls, and a two-run Compose smoke. No business approval or model calls occur in the hello path. |
| 2026-09-23 | Step 0.10 verifies the seven existing accepted ADRs, preserves their original decision dates, and aligns package paths and tracker references with the implementation. The ADK comparison remains Phase 6 work. |
| 2026-09-23 | Phase 0 complete: all ten foundation steps are implemented. Final local validation passes Ruff, strict mypy, 133 offline unit tests, 34 real integration tests, package/container builds, and isolated Compose startup with restricted API credentials and durable graph replay. CI includes the same runtime-role assertion; invoice ingestion and extraction remain Phase 1 work. |
| 2026-09-23 | Step 1.4 adds transactional append-only ledger writes, explicit version pins, and bounded run/invoice history reads. Real restricted-role tests verify atomic rollback, concurrent sequencing, immutable corrections, and pagination; all 157 offline units and 40 integrations pass. |
| 2026-09-23 | Step 1.8 pins and prepares 32 annotated synthetic Voxel51 invoices with checksummed inputs, deterministic selection, metadata-free PNGs, and versioned quality proxies. All selected images are tier A; B/C coverage and extraction quality remain unmeasured. Immutable artifacts reproduce byte-for-byte with the recorded toolchain. |
| 2026-09-23 | Step 1.5 implements the async pinned-SDK gateway client with alias policies, text guards, bounded multimodal requests, typed schema validation and provenance, deadline-aware retries, sanitized telemetry, and immutable offline cassettes. |
| 2026-09-23 | Step 1.1 adds authenticated bounded multipart uploads, PDF/PNG/JPEG signature checks, content-addressed MinIO storage, and atomic invoice/run/SYSTEM-ledger/idempotency writes. All 194 offline units and 48 real integrations pass; isolated Compose validates auth and exact replay through the restricted API role. New-key duplicate `409` is explicitly staged until step 1.3; extraction remains queued work. |
| 2026-09-23 | Step 1.3 replaces the interim duplicate conflict with original-ID `200` responses, atomic SYSTEM Reject events, and exact status/body replay. Synchronized races, duplicate replay, rollback, and preservation of original run/state are covered. The combined branch passes 273 offline units and 55 real integrations, plus the Compose upload/duplicate/replay smoke. No live model calls run. |
| 2026-09-23 | Step 1.6 adds bounded immutable-document reads and PNG/JPEG/PDF preflight, packaged extraction prompts, typed per-field observations, one technical schema-repair pass, and committed AGENT outcomes with model/prompt/source provenance. Offline synthetic SDK cassettes and real MinIO/Postgres integrations verify behavior; live model evaluation remains deferred. |
| 2026-09-23 | Step 1.7 adds pure required-field, regular-invoice sign, net-line, subtotal, per-line tax, and gross-total validation. Explicit currency rules, inclusive Decimal tolerances, isolated arithmetic context, and immutable configuration make decisions reproducible. Both PASS and FAIL commit POLICY audit evidence before returning; 401 offline units and 60 restricted-role integrations pass against merged extraction. Full workflow routing remains Phase 2 work. |
| 2026-09-23 | Step 1.2 adds a signed synthetic email webhook with bounded raw JSON and canonical attachment decoding. HMAC freshness and nonce replay controls run before ingestion; each successful nonce claim commits with the original or duplicate response. Full checks pass: 436 offline units, 65 real integrations, strict Python and TypeScript typing, and isolated Compose accept/replay/nonce/duplicate smoke. Real provider delivery remains outside this stub. |
| 2026-09-23 | Step 1.9 measures the pinned 32-image synthetic development subset through the configured LiteLLM endpoint using `gemini25flash` for vision. Tier A micro F1 is 0.7226 across 512 eligible values; 31 extracted and one malformed-output escalation. Per-field F1 ranges from 0.0560 for line totals to 0.9841 for vendor, invoice number, and tax. B/C have no selected samples, so their metrics remain unavailable. This completes Phase 1 without adding an evaluation threshold. |
| 2026-09-23 | Step 2.1 adds a Faker 40.39.0, seed-pinned synthetic ERP fixture with 12 vendors, 24 purchase orders, 12 goods receipts, and committed factual PO/receipt ground truth. The restricted-role Compose seed service inserts atomically, treats an exact rerun as a no-op, and rejects changed or partial data. The fixture hash and dependency lock make the default data reproducible. |
| 2026-09-24 | Step 2.2 adds pure, versioned three-way comparisons across invoice, PO, and cumulative goods receipts. Exact Decimal tolerance bands and signed deltas cover prices, ordered/received quantities, line amounts, and header amounts; identity and missing-data states remain explicit. A bounded async ERP reader and audited Match3Way node provide typed integration points without wiring the full graph yet. |
| 2026-09-24 | Step 2.3 adds the ten-code deterministic exception taxonomy with source and line references, input fingerprints, explicit unresolved evidence, and an audited classification node. Bank values stay out of classification output; near-duplicate and date-staleness signals are supplied by later steps. |
| 2026-09-24 | Step 2.4 adds 384-dimensional LiteLLM embedding intake, model-isolated pgvector cosine search at a versioned threshold, and atomic embedding-plus-ledger decisions. Fixed-vector tests cover near matches, model isolation, invalid responses, and audit rollback; the full graph connection follows in step 2.6. |
| 2026-09-24 | Step 2.5 adds a pure, versioned per-currency spend matrix and approval tiers, exact-duplicate/cancelled-PO blocks, and stale/closed/future-PO review controls. Coherent evidence fingerprints and an injected evaluation date make decisions reproducible; the standalone node commits POLICY evidence before routing in step 2.6. |
| 2026-09-24 | Step 2.6 wires the invoice-v1 LangGraph worker through extraction, validation, ERP matching, similarity, taxonomy, policy, an interim conservative gate, and audited review/approval/archive transitions. Synchronous Postgres checkpoints, run locks, source fingerprints, and ledger-backed replay recover committed node work; an offline fake gateway and real restricted-role database test the full auto path. The worker reads only LiteLLM URL/key/model-name variables; composite scoring and the review API remain their own plan steps. |
| 2026-09-24 | Step 2.7 replaces the interim gate for new decisions with a versioned Decimal composite score, explicit match-delta normalization, policy severity, threshold-inclusive routing, and complete audit evidence. Policy ineligibility and an operator hold always route to review; already committed provisional gate records still replay. Pure boundary tests and the full offline-gateway Postgres auto path verify behavior. |
| 2026-09-24 | Step 2.8 makes the existing runs table a durable dead-letter queue: the worker tracks RUNNING/PAUSED/COMPLETED/FAILED states, retries only typed infrastructure failures with bounded versioned backoff, and atomically records terminal failures with ledger evidence. An idempotent operator redrive requires actor and reason and resets the retry cycle. Business outcomes remain single-attempt decisions. This completes Phase 2. |
| 2026-09-24 | Step 3.1 adds analyst/manager queue reads and analyst/manager/auditor invoice detail reads with separate persona tokens, bounded keyset pagination, status/source/priority filters, and RFC 7807 errors. Review triage now commits a versioned priority/SLA exception projection, invoice status, and ledger event atomically; the detail view joins the latest run, exception, and committed evidence. Offline role tests and real restricted-role Postgres reads cover the contract. |
| 2026-09-24 | Step 3.2 adds append-only exception decision proposals from the analyst and independent manager signoff, bound to distinct persona tokens and an idempotency key. Each stage commits a decision and human ledger event atomically; the signed decision is processed by an isolated one-shot review worker that resumes the durable checkpoint and settles the run and exception after graph evidence is committed. Replays return the original record without a second write. |
| 2026-09-24 | Step 3.3 adds a typed `triage-reasoner` agent using only the direct LiteLLM URL/key and optional `LITELLM_TRIAGE_MODEL` name. A pure fact gatherer packages the authoritative taxonomy, policy, matching, validation, and extraction-escalation evidence; the model can draft a cited recommendation but cannot alter classifications or approve the invoice. Invalid citations, policy-conflicting approval advice, and gateway failures fall back to review. The queue row and AGENT ledger event commit the draft and model/prompt/evidence version pins atomically. |
| 2026-09-24 | Step 3.4 adds the React/TypeScript shell with Mantine, React Router, persona-scoped navigation, in-memory API tokens, and cache isolation on persona changes. The generated OpenAPI client is paired with Zod response contracts; the optional Compose UI proxies API paths. CI checks generation drift, types, unit behavior, production build, and the Compose proxy. A browser render verifies persona navigation and a clean console. |
| 2026-09-24 | Step 3.5 adds the exception queue and detail UI with server filters, loaded-row sorting/search, priority and SLA labels, audited three-way comparison, extraction confidence, cited triage draft, and a validated two-person decision form. Invoice detail now exposes a pending analyst proposal so manager signoff survives page reload. The form binds signoff to that proposal and reuses idempotency keys on identical retries. |
| 2026-09-24 | Step 3.6 adds a manager-only 1–90 day dashboard endpoint over a repeatable-read operational snapshot. It defines STP over resolved invoices, uses latest exceptions for aging and type counts, and reports model cost only where ledger values were observed, with coverage exposed. Dan's Recharts screen shows volume, aging, exception types, and exact cost strings, with queue drill-through. The role and aggregation contracts are covered by offline and restricted-role database tests. |
| 2026-09-24 | Step 3.7 connects Maria's Intake screen to the existing service-token upload API. A TanStack Query mutation uses XHR for upload progress and cancellation, validates accepted and problem responses with Zod, and preserves a retry key for the same file. The UI distinguishes accepted, exact-duplicate, and rejected outcomes; Maria's separate persona token polls current invoice/run status. A real Compose browser smoke accepted a synthetic PDF and then displayed the original IDs on exact-duplicate rejection. |
| 2026-09-24 | Step 3.8 adds a bounded run-progress read from the operational run and committed node audit events. It exposes an allowlisted state projection, infers the current node, and keeps storage references and bank data out of the response. The Agent Run screen polls active runs, shows all invoice-v1 nodes, and inspects recorded node outputs; Intake and Exception Review link to the run. |
| 2026-09-24 | Step 3.9 exposes auditor-only run ledger pages with actor, correction link, and graph/model/prompt/policy versions from a read-only snapshot. Priya's screen renders a timeline and full ledger table, loads further pages, and exports all run events as provenance JSON with cursor-scope checks. Invoice-wide provenance resources remain step 3.12. |
| 2026-09-24 | Step 3.10 serves bounded, versioned JSON evaluation summaries from `eval/reports/` to the auditor and platform service. The Evals screen shows measured field metrics, an experiment-log entry, per-anomaly confusion, and a τ sweep chart when those arrays exist. The current tier-A extraction baseline has no measured confusion or sweep, so those views say so; a typed pipeline report contract and tests cover future Phase 5 outputs without fabricating results. |
| 2026-09-24 | Step 3.11 adds RTL coverage for paginated queue search/sort and both sides of the four-eyes decision form. A pinned Chromium Playwright smoke drives an analyst proposal through independent manager signoff with synthetic API fixtures in CI; backend authorization remains covered by integration tests. |
| 2026-09-24 | Step 3.12 adds auditor-only run trace metadata and cross-run invoice provenance, with bounded keyset cursors and repeatable-read snapshots. Run trace omits payloads while invoice provenance includes full immutable ledger evidence and version pins. Offline access/cursor tests and restricted-role Postgres tests cover both. All Phase 3 implementation steps are complete; measured confidence-gate tuning remains dependent on the Phase 5 golden-set evaluation. |
| 2026-09-24 | Step 4.1 adds an opt-in, image-pinned Langfuse v4 stack with isolated Postgres/ClickHouse/Redis/MinIO, Prometheus, and file-provisioned Grafana. The first dashboard shows live Prometheus health; invoice metrics follow in step 4.4. Per the operator's direction, the unused Compose LiteLLM proxy, route YAML, and alternate gateway environment source are removed; runtime model traffic keeps only the direct `LITELLM_*` URL, key, and model-name variables (ADR 0008). |
| 2026-09-24 | Step 4.2 adds sanitized OpenTelemetry parent spans for durable graph runs, every invoice/hello graph node, and the tool operations invoked by the live workflow. API and worker entry points can opt into batched OTLP/HTTP export with standard OTel endpoint/header variables; an absent endpoint leaves export disabled. Offline tests assert parent-child correlation, identifiers, error types, and exclusion of payload text. |
| 2026-09-24 | Step 4.3 traces each gateway chat and embedding call as a Langfuse-compatible OTLP observation, carrying model identity, version pins, token usage, exact reported cost, latency, retry count, and typed errors without content. The existing direct LiteLLM endpoint remains the only model route; ADR 0009 replaces the planned proxy callback because it would require additional LiteLLM configuration. |
| 2026-09-24 | Step 4.4 adds low-cardinality OTel API request and worker gateway metrics, a Prometheus API endpoint, bounded one-hour LiteLLM spend-log snapshots using the existing URL and key, and provisioned cost/latency dashboards. Live Compose smoke verified the API scrape, authorized spend-log read, Grafana provisioning, and one-shot worker OTLP samples; missing cost headers or inaccessible spend logs remain explicitly unavailable. |
| 2026-09-24 | Step 4.5 completes Phase 4 with restricted-by-default task/sensitivity routing, optional model-name fallbacks for infrastructure failures, a public/text-only pgvector semantic cache, and advisory Decimal per-run budget alerts. New model routes use only `LITELLM_*_MODEL` variables in `.env.example`; ADR 0010 records cache and routing limits. Ruff, strict mypy, 538 offline units, and 91 real integrations pass; Compose confirms the cache migration, Prometheus rule, and Grafana panels. |
| 2026-09-24 | Step 5.1 builds golden/v1.0.0 from 50 newly selected Voxel51 extraction-only images and 450 ERP-backed synthetic images. It pins 350 clean and 150 anomalous cases, 30 visual hard negatives, ten published anomaly counts, a 100/400 development/held-out split, source and document checksums, and a 399-order ERP snapshot. The builder verifies committed manifests byte-for-byte. |
| 2026-09-24 | Step 5.2 adds an API-driven Compose runner with preflighted document hashes, idempotent uploads, restricted-role golden ERP seeding, a bounded batch worker, and auditor readback. A committed three-call cassette smoke ran in a fresh isolated Compose project and paused at human review with nine ordered ledger events; no live model call was made. |
| 2026-09-24 | Step 5.3 adds eight primary golden metrics with explicit denominators and coverage. It requires exact injected-code detection, scores only known extraction labels, includes embedding cost in immutable similarity evidence and dashboard totals, and uses three independent live runs for audited auto-approval p95. Recorded smoke and partial selections remain visibly ineligible for release claims; live suite measurements follow after an embedding model route is configured. |
| 2026-09-24 | Step 5.4 adds per-anomaly confusion, per-field/per-tier extraction F1, score calibration bins, and a policy-preserving τ sweep. A versioned advisory triage rubric calls the existing gateway via an eval-only agent and the optional `LITELLM_JUDGE_MODEL` name; judge output is source- and evidence-bound, with unavailable results kept separate from zero scores. No live model-quality measurement is claimed. |
