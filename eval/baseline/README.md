# Extraction development baseline

Step 1.9 measures the existing extraction agent on the pinned 32-image synthetic
[Voxel51 development subset](../datasets/README.md). It is a measured development
baseline, not a held-out evaluation or a release gate.

Create ignored `.env` from `.env.example` and set `LITELLM_API_BASE`,
`LITELLM_MASTER_KEY`, and `LITELLM_MODEL`. Set `LITELLM_EXTRACT_MODEL` when the
general model does not accept images. The runner sends that model name directly
to the configured LiteLLM endpoint through the project's gateway client. It
does not load a LiteLLM proxy YAML file. The gateway model list and a one-image
probe should be checked before running the full subset.

```bash
uv run python -m eval.baseline.run \
  --dataset eval/data/voxel51-v1 \
  --reference-report eval/reports/voxel51-v1.json \
  --output eval/reports/extraction-baseline-v1.json
```

The runner verifies the committed manifest digest, every source annotation,
every prepared PNG checksum, and that prepared paths stay within the dataset
before it calls a model. It then uses the packaged extraction prompt and the
same schema validation and one repair attempt as the production extraction
agent. Each extraction outcome emits its agent audit event into the disposable
evaluation sink; the committed report contains statuses, model/prompt versions,
aggregate usage and scores, without invoice text or credentials. Failures to
extract count as missed annotated fields.

## Scoring contract

Only seven annotated fields are eligible. The source `seller_name`,
`invoice_number`, `invoice_date`, and `subtotal.tax` map to extracted
`vendor_name`, `invoice_number`, `invoice_date`, and `tax_amount`. Source item
`description`, `quantity`, and `total_price` map to extracted item
`description`, `quantity`, and `line_total` by row index. Extra extracted rows
count as false positives. There is no fuzzy row matching, so row order errors
affect the score. The source does not establish whether `total_price` is net;
that field's score measures numeric correspondence only, not tax treatment.

Text is Unicode NFKC normalized, case folded, and whitespace collapsed;
dates use the source month/day/year form; numbers use exact Decimal equality
after normalizing comma or dot decimal separators. A wrong value counts as one
false positive and one false negative. Unlabeled fields, including currency,
IBAN, unit price, and gross amount, are excluded rather than treated as
negative examples. The report includes TP, FP, FN, precision, recall, and F1
for each eligible field and their micro aggregate.

All 32 selected images are quality tier A. The report records B and C as
unavailable (`null`); it does not extrapolate A scores to those tiers. The
quality tiers are image heuristics, not independently judged readability.
