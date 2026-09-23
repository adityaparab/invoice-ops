# Exception taxonomy v1

Step 2.3 defines a deterministic classification boundary for already collected
invoice evidence. `classify_exceptions` performs no I/O, model calls, clock reads,
or scoring. A caller supplies the extraction, its validation result, its three-way
match result, the ERP vendor bank account if available, and explicit duplicate or
staleness signals. Validation and matching fingerprints must identify the same
extraction. Inputs and policy versions are hashed in the result.

| Code | Evidence that creates it |
| --- | --- |
| `DUP_EXACT` | Ingest's confirmed content-hash duplicate signal |
| `DUP_NEAR` | A confirmed similarity signal; detection is step 2.4 |
| `MISSING_PO` | Three-way match found no ERP snapshot for the extracted PO |
| `BANK_CHANGE` | Extracted IBAN differs from the supplied ERP vendor IBAN after whitespace/case normalization |
| `CCY_MM` | Three-way currency identity mismatch |
| `PRICE_MM` | Three-way unit-price mismatch beyond the match policy tolerance |
| `QTY_MM` | Ordered/received quantity or receipt/order quantity mismatch |
| `TAX_ERR` | Validation tax mismatch or out-of-range tax rate |
| `MATH_ERR` | Validation arithmetic/sign/quantity issue |
| `STALE_PO` | ERP PO is closed/cancelled, or the caller supplies a policy staleness signal |

The code list is deduplicated in the order above. Each finding retains the source,
field, optional line number, and original check/issue index. Identity mismatches
outside currency, unknown checks, unclassified subtotal/line mismatches, and
validation issues without a matching code remain in `unresolved`; they are never
silently treated as clean. Missing bank evidence, validation, or matching runs also produce
`unresolved` references. `EXCEPTION` means at least one code was found;
`UNCLASSIFIED` means no code but unresolved evidence; `CLEAN` requires neither.

The result stores only a fingerprint of the supplied ERP IBAN, not the account
number. Its append-only `classification.completed` audit event pins the taxonomy
policy version and records the full result. This node is an integration point;
graph routing arrives in step 2.6. Stale-date policy and near-duplicate detection
are supplied by steps 2.5 and 2.4 respectively.
