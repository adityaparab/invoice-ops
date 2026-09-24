# Golden evaluation dataset

Step 5.1 publishes `golden/v1.0.0`, a seed-pinned set of 500 synthetic invoice images and
their typed labels. Step 5.8 corrects gross Voxel51 row labels in
[`golden/v1.0.1`](../eval/golden/v1.0.1/manifest.json). All 500 sample IDs, image hashes,
splits, anomaly assignments, and ERP records remain identical. Voxel51 only annotates
gross row amounts, so its net `line_total` labels are now unknown. The builder makes no
model calls. [The committed manifest](../eval/golden/v1.0.1/manifest.json)
records each document checksum, its split, origin, extraction labels, quality tier, and
expected anomaly code. [The ERP snapshot](../eval/golden/v1.0.1/erp.json) supplies the
purchase orders and receipts for the synthetic routing cases. Document PNGs live in
ignored `eval/data/` and are recreated from the locked toolchain.

| Segment | Count | Routing ground truth |
| --- | ---: | --- |
| Re-labeled Voxel51 clean invoices | 50 | No: extraction only; upstream has no PO or currency labels |
| Synthetic clean invoices | 300 | Yes: matching ERP snapshot |
| Synthetic anomalous invoices | 150 | Yes: seeded taxonomy code |

The 350 clean samples include 30 synthetic visual hard negatives: eight rotations,
eight skews, seven stamps, and seven faint-print documents. The split has 100
development and 400 held-out samples, stratified across origin, visual stressors,
and anomaly code. Exact and near-duplicate pairs remain in the same split. The
32-image Voxel51 extraction baseline is excluded by source ID, path, and source
hash; it is never part of either golden split. The development split may guide
prompt or threshold work; the held-out split is reserved for release evaluation.

The `identifier-ocr@v1` correction was calibrated only on the 90 synthetic
development documents: anchored bank text was exact in 90/90 and PO text in
89/90 before the 70/100 confidence cutoff. The one wrong PO was below that
cutoff. These observations justify a development experiment, not an accuracy
claim about real invoices or the held-out split. The OCR candidate and its
application are preserved in each extraction audit event.

The source images are synthetic. [Voxel51's dataset card](https://huggingface.co/datasets/Voxel51/high-quality-invoice-images-for-ocr)
declares ODbL and attributes the original Kaggle corpus and FiftyOne port. This
project pins source revision `d21f03cfeea2b330e15a229883c66d7ebece8e69` and metadata
SHA-256 `79cb36846bf28f41636d795d2f85fd8b7ef99a8e9807f6ddc8e6f0d7c883ae7d`.
Re-labeling maps only visible, annotated fields; missing PO, currency, IBAN,
unit price, and tax rate stay unknown. No unknown field is scored as a negative.

## Anomaly prevalence assumptions

| Code | Count | Share of anomalous set |
| --- | ---: | ---: |
| `DUP_EXACT` | 20 | 13.3% |
| `DUP_NEAR` | 15 | 10.0% |
| `PRICE_MM` | 22 | 14.7% |
| `QTY_MM` | 20 | 13.3% |
| `MISSING_PO` | 16 | 10.7% |
| `BANK_CHANGE` | 10 | 6.7% |
| `CCY_MM` | 10 | 6.7% |
| `TAX_ERR` | 16 | 10.7% |
| `MATH_ERR` | 11 | 7.3% |
| `STALE_PO` | 10 | 6.7% |

These weights are explicit test assumptions, not observed production prevalence.
`DUP_EXACT` repeats a clean document's bytes; `DUP_NEAR` changes only a small
visual mark. The other codes mutate a single invoice or ERP fact from a valid
base case. `MISSING_PO` has no ERP order; all other non-duplicate synthetic
cases have one purchase order and one receipt. Most orders are partially
received so a valid invoice can match without a closed-PO policy violation.

## Reproduce and verify

```bash
uv sync --locked
uv run python -m eval.golden.prepare
```

The command downloads 50 public source JPEGs, normalizes them to metadata-free
PNGs, renders the 450 synthetic PNGs, and writes `eval/data/golden/v1.0.1/`.
It then verifies the generated manifests byte-for-byte against the committed
copies in `eval/golden/v1.0.1/`. Existing artifact bytes are never overwritten.
The pinned Python dependencies include Pillow; the manifest records its and
zlib's versions. Source downloads have size and elapsed-time bounds. A failed
or interrupted build cannot publish a complete manifest with unverified bytes.

## Scoring boundaries

The [Compose runner](../eval/runners/README.md) collects API, worker, and ledger
evidence for the suite. [`eval/metrics.py`](../eval/metrics.py) scores the eight
primary measures. Diagnostics and the [CI gate](../eval/ci_gate.py) consume those
reports.
Extraction scoring uses all fields with known labels. Exact duplicate uploads
are excluded from extraction F1 because they do not invoke extraction. Wrong
observed values count one false positive and one false negative; unknown gold
labels are skipped. Money F1 covers subtotal, tax, total, line unit price, and
line total. Routing, exception recall, false escalation, and straight-through
processing use only the 450 ERP-backed samples; the 50 Voxel51 invoices are
extraction-only.
Duplicate samples must run after their parent in each split. Anomalies may
produce additional secondary findings, so the injected code is the required
minimum detection label. Hard negatives test extraction robustness, not an
extra business exception.

Exception recall requires each sample's injected code in `classification.completed`;
an exact duplicate is detected by the upload's content-hash duplicate signal.
The denominator is 150 anomalies. False escalation counts a clean synthetic
invoice that does not reach audited auto-approval, over 300 clean synthetic
invoices. Routing accuracy expects audited auto-approval for clean invoices,
ingest rejection for exact duplicates, and human review for other anomalies,
over all 450 routing cases. STP is the audited auto-approval share of the 300
clean synthetic invoices; that denominator keeps the 70% target attainable.

Cost per invoice sums observed extraction, embedding, and triage gateway cost
for each selected invoice. Duplicate uploads cost zero for their own attempt.
If any invoked model call lacks cost evidence, the metric is unavailable and
coverage shows how many invoices are complete.
The gateway prefers LiteLLM's total response-cost header. When it is absent,
it uses original cost minus discount plus margin only if all three adjustment
headers are present and valid. Billed malformed responses and conservative
triage fallbacks keep their observed cost in the ledger. Missing headers stay
unavailable rather than becoming zero.

The live runner uploads up to `--workers` invoices, processes that batch, and only then
uploads the next. Duplicate children wait for their parent batch. This bounded arrival
schedule measures service latency without adding an artificial wait from uploading the
entire corpus ahead of the workers.

P95 latency uses the elapsed time between `ingest.accepted` and
`approval.auto_granted`, from three distinct
live runs over the same selection. It is unavailable when a run lacks an
auto-approval, a timestamp, or independent run IDs. P95 uses the nearest-rank
method over audited auto-approved invoices from the three runs. The versioned
target and direction for each metric travel in the output. A one-run development
score can guide iteration, but only a full live 500-invoice report has
`complete_suite=true`; recorded cassette runs are smoke evidence, not model
quality measurements. Every metric reports its numerator, denominator or
evidence coverage so a missing value cannot be mistaken for zero.

```bash
uv run python -m eval.metrics \
  --input eval/data/runs/live-1.json \
  --input eval/data/runs/live-2.json \
  --input eval/data/runs/live-3.json \
  --output eval/data/metrics/golden-v1.0.1.json
```

## Diagnostics and triage rubric

[`eval/diagnostics.py`](../eval/diagnostics.py) reads one pipeline report and
emits per-code TP/FP/FN/TN counts, exact-value field F1 by A/B/C visual tier,
ten equal-width score bins, and 101 threshold points from τ=0 to τ=1. Empty
tier/field bins remain unavailable. Calibration compares the composite score
with the observed clean share; the score is a ranking signal, not a probability.
The τ sweep keeps policy eligibility fixed and changes only the score cutoff.
Its routed-exception recall asks whether an anomaly would avoid auto-approval;
the primary recall metric instead requires the injected taxonomy code.

```bash
uv run python -m eval.diagnostics \
  --input eval/data/runs/live-1.json \
  --output eval/data/diagnostics/live-1.json
```

The optional triage judge uses only the direct LiteLLM URL, key, and the new
`LITELLM_JUDGE_MODEL` model-name variable documented in `.env.example`.
`triage-judge@v1` scores evidence support, action safety, and clarity from
zero to two each. Calls go through `src/agents/eval_judge.py` and the existing
gateway. Its immutable report pins the source pipeline checksum, request
evidence checksum, prompt version, model version, and observed cost. A missing
model route or failed call never becomes a zero quality score; coverage stays
explicit. To run it against a synthetic report and join its results:

```bash
uv run python -m eval.judge_triage \
  --input eval/data/runs/live-1.json \
  --output eval/data/judges/live-1.json
uv run python -m eval.diagnostics \
  --input eval/data/runs/live-1.json \
  --judge-report eval/data/judges/live-1.json \
  --output eval/data/diagnostics/live-1.json
```

## Model-class reports

Step 5.5 requires an explicit `local-dev` or `openai-prod` tag on every new
live pipeline run. This is an operator-declared experiment label. Configure
the direct LiteLLM URL, key, and model-name variables for the intended class
before each run; the tag does not silently reroute a model. Each pipeline,
primary-metric, diagnostic, and triage-judge report carries that class. The
primary report also lists the gateway model versions pinned by audited model
events. Historical v1 reports without a class remain readable but cannot
claim a complete class measurement.

Score three independent live runs for each class with `eval.metrics`, then
compare the resulting metric reports:

```bash
uv run python -m eval.model_classes \
  --local eval/data/metrics/local-dev.json \
  --production eval/data/metrics/openai-prod.json \
  --output eval/data/metrics/model-class-comparison.json
```

The comparison pins both source report checksums, requires the same golden
manifest and selected sample count, preserves unavailable values, and shows
`openai-prod minus local-dev` for each primary metric. It marks itself complete
only when both classes have full 500-invoice, three-run live reports with all
eight primary measurements observed. No class-level results are inferred from
the recorded smoke.

The synthetic documents use one line item and repeated template structure;
those constraints limit claims about real invoice diversity. The published
split and label eligibility keep that limitation visible in every report.

## CI regression gate

On each pull request, the gate compares the committed
`eval/reports/golden-v1.0.1-openai-prod.json` report with the same path at the
PR's base commit. Both reports must be tagged `openai-prod`, cover the same
golden manifest, contain all eight observed primary metrics, and represent
three independent live runs of all 500 invoices. It rejects a candidate below
any versioned floor or more than 0.5 percentage points worse than main on a
rate. Cost and latency have different units, so their allowed regression is
0.5% of the versioned target: $0.0002/invoice and 0.225 seconds. Equality at
the tolerance passes. The CI job writes a Markdown delta table to the job
summary and comments it on same-repository PRs using `gh`.

The first committed live report establishes the baseline: CI checks all floors
and completeness against that report, then starts base-versus-PR comparisons
after it merges. Until that report exists, the CI job explicitly says the live
gate awaits evidence. The recorded cassette smoke is never used to pass a
model-quality gate. To compare complete reports locally:

```bash
uv run python -m eval.ci_gate \
  --baseline eval/data/metrics/main-openai-prod.json \
  --candidate eval/reports/golden-v1.0.1-openai-prod.json \
  --comment-file eval/data/runs/ci-gate.md \
  --output eval/data/runs/ci-gate.json
```

## Versioned report publication

[`eval/publish_report.py`](../eval/publish_report.py) converts a matching live
primary report and diagnostic report into the versioned Evals-screen schema.
It keeps unavailable measurements out of metric rows, labels partial evidence,
shows per-anomaly confusion and eligible threshold points, and converts p95
seconds to milliseconds for the UI. The primary and diagnostic source JSONs
remain separate, preserving detailed denominators and calibration. A
publication command is:

```bash
uv run python -m eval.publish_report \
  --metrics eval/data/metrics/openai-prod-development.json \
  --diagnostics eval/data/diagnostics/openai-prod-development.json \
  --report-id golden-openai-prod-development-v1 \
  --title "Golden development measurement" \
  --output eval/reports/pipeline-eval-openai-prod-development-v1.json
```

The experiment log at `eval/reports/experiment-log-v1.json` links each
publication by `report_id` and records its hypothesis, change, observation,
and decision. Development or partial measurements never become the production
CI baseline.

The first live development publication is
[`pipeline-eval-openai-prod-development-v01.json`](../eval/reports/pipeline-eval-openai-prod-development-v01.json),
with separate [primary metrics](../eval/reports/primary-openai-prod-development-v01.json)
and [diagnostics](../eval/reports/diagnostics-openai-prod-development-v01.json).
It covers 100 of 500 cases and one run. Exception recall was 0.9667 and clean
false escalation was 1.0000; 48 near-duplicate and 26 bank-change false
positives explain much of the review volume. Three repeated-citation failures
made cost incomplete, and p95 requires two more independent runs. The triage
citation bug is fixed in the same step. Step 5.8 addresses the remaining
development failures in the full live release baseline below.

## First full live baseline

The [primary report](../eval/reports/golden-v1.0.1-openai-prod.json) covers all
500 golden invoices in three independent live Compose runs. Each run used a fresh
Postgres and MinIO volume, eight workers, the same application build, and the
configured LiteLLM URL and key with the `gpt5nano`, `gpt4omini`, and
`gemini-embedding` model-name routes. None had a worker error; all 500 invoices
in each run had complete billed-cost evidence. The
[diagnostic report](../eval/reports/diagnostics-golden-v1.0.1-openai-prod.json)
scores the first run's per-code, per-field, calibration, and threshold detail;
the [Evals-screen report](../eval/reports/pipeline-eval-openai-prod-release-v1.json)
combines that diagnostic with the three-run primary score. The raw run reports
remain in ignored `eval/data/runs/`.

| Primary measure | First-run result | Floor |
| --- | ---: | ---: |
| Exception recall | 149/150 = 0.9933 | ≥0.98 |
| Clean false escalation | 10/300 = 0.0333 | ≤0.05 |
| Field F1 | 0.9732 | ≥0.95 |
| Money-field F1 | 0.9913 | ≥0.97 |
| Routing accuracy | 440/450 = 0.9778 | ≥0.95 |
| Straight-through approval | 290/300 = 0.9667 | ≥0.70 |
| Billed cost per invoice | $0.00122339726, 500/500 covered | ≤$0.04 |
| Audited auto-approval p95 | 30.872508 seconds, 875 observations across three runs | ≤45 seconds |

The second and third runs detected 149/150 and 150/150 anomalies respectively;
their clean false-escalation counts were 8/300 and 7/300. The primary report
uses the first run for quality and cost measures and pools audited latencies
from all three. These are measurements on synthetic and re-labeled public
invoices under the stated model routes, not estimates for real vendor traffic.
