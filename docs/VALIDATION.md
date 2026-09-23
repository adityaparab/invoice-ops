# Deterministic validation (step 1.7)

`tools.validation.validate_invoice(InvoiceExtraction, ValidationConfig)` is pure: the same typed
extraction and versioned configuration produce the same result. It performs no network, database,
clock, randomness, or model calls. `PASS` means the configured completeness and arithmetic controls
passed; it is not payment approval, a tax-law assessment, or the later confidence gate.

The default `invoice-validation@v1` is an explicit example policy for regular invoices. Negative
amounts and nonpositive quantities fail; credit notes require a separately reviewed policy. Tax rates
are fractions from zero through one, inclusive (`0.20` means 20%). This bound expresses the example
policy rather than a claim about jurisdiction-specific tax rules.

## Required evidence

The validator requires vendor name, invoice number, currency, invoice date, net subtotal, tax amount,
gross total, and at least one line. Each line requires description, quantity, unit price, fractional
tax rate, and net line total. Null or blank required values produce `REQUIRED_FIELD` issues. Missing
operands skip dependent arithmetic checks; they never become zero. Missing PO, bank account, vendor
tax ID, and due date are allowed. Vendor-master checks, PO matching, date policies, confidence
thresholds, and exception routing belong to later steps.

## Arithmetic policy

All comparisons use `Decimal`, with a fresh precision-60 context and explicit `ROUND_HALF_UP`.
Extraction bounds operands to at most 18 digits and invoices to 500 lines, so multiplication and
aggregation fit this context. Caller precision, rounding, exponent limits, and traps cannot change
the decision. Binary floating-point policy inputs are rejected.

| Currency | Rounding quantum | Inclusive absolute tolerance |
|---|---|---|
| EUR, GBP, PLN, USD | 0.01 | 0.01 |
| JPY | 1 | 1 |
| KWD | 0.001 | 0.001 |

Unconfigured currencies produce `UNSUPPORTED_CURRENCY`; there is no implicit two-decimal fallback.
Configuration is injected, immutable, and recorded in full with a canonical SHA-256 fingerprint.
Custom currency rules and tolerances require an explicit policy version. Required-field, sign,
rounding, or algorithm changes must also bump that version.

Checks run in stable input order:

1. Round `quantity × unit_price` to the currency quantum and compare with the observed net line total.
2. Sum observed net line totals and compare with the header subtotal.
3. Round each `net line total × fractional tax rate` separately, sum those rounded taxes, and compare
   with the header tax amount. This is explicitly per-line tax rounding, not rounding one aggregate.
4. Compare `subtotal + tax_amount` with the gross invoice total.

A difference exactly equal to tolerance passes; a larger absolute difference fails. Arithmetic
issues include expected and actual values, signed difference, tolerance, and a one-based line number
where applicable. Invalid or missing values remain issues even when their arithmetic checks cannot
run. The extractor's confidence values do not affect these deterministic controls.

## Audited node and contracts

`graph.nodes.validate.ValidateNode` accepts a `ValidationRequest` containing the typed extraction,
invoice ID, run ID, and trace ID. It invokes the pure validator, then awaits the injected ledger
`AuditSink`. Both PASS and FAIL produce one `validation.completed` POLICY event with node `Validate`.
The event records the result, complete configuration, configuration hash, and canonical extraction
hash. It pins the policy version and explicit `not-applicable@v1` model/prompt versions; the ledger
writer supplies the matching run's graph version. Vendor names, bank details, and raw document data
are absent from validation evidence and logs.

With `TransactionalAuditSink`, the event commits in a short transaction before the node returns.
Audit failures and cancellation propagate; business failures return typed FAIL results and are never
retried. Each standalone invocation is a new audited attempt. The full worker, durable graph replay,
and routing remain Phase 2 work; this step does not alter the hello graph or upload processing.

Pydantic contracts live in `schemas/validation.py`, with matching JSON-wire Zod contracts in
`frontend/src/schemas/validation.ts`. Decimal evidence uses canonical fixed-point strings. Configuration
and input hashes fingerprint canonical JSON; they are reproducibility identifiers, not signatures.

Offline tests cover every arithmetic check, unknown and absent evidence, optional fields, currency
quanta, tax rounding, both sides of tolerance boundaries, extreme bounded operands, 500-line sums,
ambient Decimal-context independence, policy/input fingerprints, audit failures, and cancellation.
Real Postgres integration tests use the restricted runtime role to verify committed PASS/FAIL
events, Decimal evidence and version pins, and rollback after a staged audit failure.
