# Golden evaluation dataset

Step 5.1 publishes `golden/v1.0.0`, a seed-pinned set of 500 synthetic invoice images and
their typed labels. The builder makes no model calls. [The committed manifest](../eval/golden/v1.0.0/manifest.json)
records each document checksum, its split, origin, extraction labels, quality tier, and
expected anomaly code. [The ERP snapshot](../eval/golden/v1.0.0/erp.json) supplies the
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
PNGs, renders the 450 synthetic PNGs, and writes `eval/data/golden/v1.0.0/`.
It then verifies the generated manifests byte-for-byte against the committed
copies in `eval/golden/v1.0.0/`. Existing artifact bytes are never overwritten.
The pinned Python dependencies include Pillow; the manifest records its and
zlib's versions. Source downloads have size and elapsed-time bounds. A failed
or interrupted build cannot publish a complete manifest with unverified bytes.

## Scoring boundaries

The [Compose runner](../eval/runners/README.md) collects API, worker, and ledger
evidence for the suite. [`eval/metrics.py`](../eval/metrics.py) scores the eight
primary measures; diagnostics and the CI gate follow in later Phase 5 steps.
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
coverage shows how many invoices are complete. P95 latency uses the elapsed
time between `ingest.accepted` and `approval.auto_granted`, from three distinct
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
  --output eval/data/metrics/golden-v1.0.0.json
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

The synthetic documents use one line item and repeated template structure;
those constraints limit claims about real invoice diversity. The published
split and label eligibility keep that limitation visible in every report.
