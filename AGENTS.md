# AGENTS.md — InvoiceOps Agent

Instructions for AI agents (and humans) contributing code to this repository. Follow these rules unless the user explicitly overrides them. The plan of record is [`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md) — check off steps there as they complete; authoritative design specs are `README.md` and `docs/`.

## Project context

Agentic invoice-processing platform: FastAPI service + LangGraph state machine, Postgres (+pgvector) + MinIO, all LLM traffic via a LiteLLM proxy (OpenAI-compliant API, virtual model aliases), full React/TypeScript front end, eval-gated CI. **Deterministic controls at the edges, LLM intelligence in the middle, humans in the loop for consequential decisions, everything auditable.** That principle governs code structure as much as architecture.

## Code quality principles (non-negotiable)

Every change must be **clean, modular, reusable, testable, and production-grade**:

- **Clean** — small functions doing one thing; names that say what they mean; comments only for constraints the code can't express; dead code and TODO-without-ticket deleted, not parked.
- **Modular** — one responsibility per module; dependencies point downward (see Architecture boundaries). No circular imports, ever.
- **Reusable** — extract a helper the *second* time it's duplicated, not the third; shared contracts live in schema modules, not inline dicts.
- **Testable** — no test requires luck: inject dependencies (DB sessions, clock, gateway client, storage), pin seeds, record LLM responses as cassettes. If a function needs a real network call to test, its seams are wrong.
- **Production-grade** — every failure path handled explicitly; no bare `except:`; retries only for infra errors (business failures never retry); secrets only via env; observability spans/log fields on anything slow or risky; migrations reversible.

## Architecture boundaries (src/)

```
src/api/           FastAPI routes, dependencies, middleware — no business logic
src/graph/         LangGraph state machine (state.py + nodes/) — orchestration only
src/agents/        Extraction + triage agents — the ONLY places LLMs are called
src/tools/         OCR/VLM tool, ERP repo, policy engine, dedupe — deterministic
src/ledger/        Append-only audit ledger writer/reader
src/gateway_client/  Thin openai-SDK wrapper over LiteLLM — the ONLY model doorway
src/obs/           OTel setup, exporters, logging config
```

- Imports flow `api → graph → agents/tools → gateway_client/ledger/obs`. Never upward, never sideways past a boundary. `tools` must never import `agents`.
- **Only `src/agents/` and `src/gateway_client/` may issue LLM calls.** A model call anywhere else is a bug.
- **Matching and policy logic (`src/tools/`) is pure, deterministic code** — no model calls, no wall-clock reads, no randomness; tolerances and thresholds come from versioned config so decisions are reproducible.
- Pydantic v2 models are the contract layer: `src/graph/state.py` (GraphState), and one schema module per API resource. Zod schemas in `frontend/` mirror them; when a Pydantic model changes, the Zod twin changes in the same commit.

## Python standards

- Python 3.12, dependencies managed **only** with `uv` (`uv add`, `uv run`, `uv lock`). Never pip-install into the venv by hand.
- Type hints on every public function; `mypy --strict` passes; `ruff check` + `ruff format` clean. Both run in CI — run locally before committing.
- Async discipline: no blocking I/O inside async paths (use `asyncio`-compatible clients; `run_in_executor` for unavoidable sync calls); FastAPI endpoints async by default.
- Configuration via `pydantic-settings` classes; no module-level env reads scattered through code.
- Errors: RFC 7807 problem+json at the API layer; typed exception hierarchy internally; error messages carry run_id/trace context.
- Logging: structured (key=value or JSON), never `print()`; log fields include `run_id` where a graph run is in context.
- Money, tax, quantities: `Decimal`, never `float`. Dates: timezone-aware `datetime` (UTC internally).
- Idempotency: every externally-triggered mutation honors an idempotency key; every ingest dedupes on `content_hash`.

## Frontend standards (frontend/)

- TypeScript `strict`; `any` (and `as any`) require a comment justifying survival. Zod parses every API response at the boundary — no trusting the wire.
- Server state only through TanStack Query (no ad-hoc `useEffect` fetching). Mutations invalidate the specific query keys they affect.
- Components: small, props typed; feature logic in hooks, not components; Mantine components for UI primitives — don't hand-roll modals/tables/notifications.
- **Styling lives in CSS Modules only — never in component code.** Every visual style (layout, spacing, colors, typography, sizing) is defined in a colocated `*.module.css` file imported as `styles` and applied via `className`. No inline `style` props and no Mantine visual style props (`mt=`, `p=`, `c=`, `bg=`, `fz=`, `w=` …) in ts/tsx — if it draws, it belongs in the module CSS. Only exceptions: the app-level Mantine theme token object in the provider, the single global stylesheet imported once at the app entry, and data-derived values passed as CSS custom properties (`style={{ '--bar-fill': value }}`) consumed by the module CSS.
- No business logic duplicated from the backend — the API is the source of truth; derive in React only what is presentation.

## Testing rules

- Pyramid per phase: unit (fast, mocked edges) → integration (testcontainers Postgres/MinIO, real graph runs with VCR cassettes) → eval (golden dataset through the real stack).
- New code ships with tests in the same PR. Bug fixes ship with the failing test first.
- No unit test touches the network. LLM responses are cassettes (record once, replay offline); eval runs are the only place real models run, and they run in CI, not local pre-commit.
- Determinism: `random`/`faker` seeds pinned; time frozen via injected clock where it matters; cassette recordings committed.
- Frontend: Vitest + RTL for forms/queue logic; one Playwright happy-path smoke in CI.

## Audit & data rules

- `ledger` and `decisions` tables are **append-only** — no UPDATE/DELETE paths, no ORM method that makes them possible. Every entry pins model/prompt/policy versions.
- Synthetic data only. Never commit real vendor names, bank details, or API keys. Seed generators are versioned and seed-pinned so datasets reproduce bit-for-bit.

## Git & workflow

- Conventional commits (`feat:`, `fix:`, `refactor:`, `test:`, `chore:`, `docs:`). Small commits, one logical change each.
- **One branch per plan step.** Every step from `IMPLEMENTATION_PLAN.md` is implemented on its own dedicated branch, named after the step: `p<phase>/<step-number>-<short-slug>` — e.g. `p0/06-alembic-migrations`, `p3/05-exception-review-screen`. Branch from `main`, keep the branch scoped to that single step (spillover into another step = a separate branch/PR). One PR per branch, linked to the step in its description.
- **All GitHub interaction goes through the `gh` CLI** (already configured and authenticated) — never the web UI or hand-crafted API calls: `gh pr create`, `gh pr checks` (wait for CI before merging), `gh pr merge`, `gh pr comment`, `gh issue ...`, `gh run watch` for pipeline debugging. Plain `git` is only for local operations and pushing the branch itself.
- PRs pass CI (ruff → mypy → unit → integration → eval gate) before merge via `gh pr checks`. The eval gate failing means the PR fails — never weaken a metric threshold to make CI pass; investigate or revert; if a metric definition itself is wrong, that's a reviewed change with an experiment-log entry, not a quiet edit.
- Scope creep guard: payment execution, vendor onboarding, and real ERP writeback are non-goals — if a change drifts toward them, stop and flag it.

## Definition of done (per task)

1. Code compiles, `ruff` + `mypy --strict` clean, all tests green.
2. New behavior covered by tests; failure paths handled; types tight.
3. Observability present (span/log fields/units) where the code does I/O or decisions.
4. Ledger entries emitted for any new decision-relevant step.
5. Docs updated if contracts/architecture changed (ADR if a decision changed).
6. Work merged via `gh` after `gh pr checks` passes; checkbox ticked in `IMPLEMENTATION_PLAN.md` with a progress-log row if a phase completed.
