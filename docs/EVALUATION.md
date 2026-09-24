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
evidence for the suite; metric definitions and the CI gate follow in later
Phase 5 steps. Extraction scoring uses all fields with known labels. Routing,
exception recall, false escalation, and straight-through processing use only
the 450 ERP-backed samples; the 50 Voxel51 invoices are extraction-only.
Duplicate samples must run after their parent in each split. Anomalies may
produce additional secondary findings, so the injected code is the required
minimum detection label. Hard negatives test extraction robustness, not an
extra business exception.

The synthetic documents use one line item and repeated template structure;
those constraints limit claims about real invoice diversity. The published
split and label eligibility keep that limitation visible in every report.
