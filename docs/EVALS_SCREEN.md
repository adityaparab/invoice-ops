# Evals screen and report contract

`GET /v1/evals/reports` reads committed JSON under `eval/reports/`. The API
allows Priya's auditor token and the Platform Engineer service token. It parses
the existing `extraction-baseline-v1.json` into a small, typed summary and
reads `experiment-log-v1.json` for hypotheses, changes, observations, and
decisions. The Docker image includes only `eval/reports/` from the eval tree.
Files are read off the async event loop, bounded to 1 MB each, and cannot
resolve outside the configured report directory.

The current baseline is a 32-image tier-A development measurement. Its field
F1 values and eligible/TP/FP/FN counts are shown as recorded. Tier B/C,
per-anomaly confusion, and the τ sweep have not been measured, so the screen
labels those views as unavailable. No values are inferred for them.

Future pipeline reports can be added as `pipeline-eval-*.json` with
`report_version: "pipeline-eval@v1"` and the `EvalReport` schema from the
OpenAPI contract. Each report carries exact decimal strings, model and dataset
versions, metric rows, per-anomaly confusion counts, τ sweep points, and
caveats. The screen renders those arrays without changing metric definitions;
Phase 5 owns their generation, validation, and CI thresholds.
