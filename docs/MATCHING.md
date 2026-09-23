# Deterministic three-way comparison (step 2.2)

The matcher compares one typed invoice extraction with a typed snapshot of one
synthetic ERP purchase order, its vendor, and up to 100 goods receipts. The
[ERP repository](../src/invoiceops_agent/tools/erp_repository.py) reads the
snapshot through a caller-owned async Postgres connection in one SQL statement,
so vendor, PO, and receipts share one database snapshot. ERP contracts bound
line counts, field lengths, decimal precision, and timestamps. The
[pure matcher](../src/invoiceops_agent/tools/matching.py) receives those values
and a frozen, versioned `MatchConfig`; it performs no I/O, model call, clock
read, or random draw. The `Match3WayNode` writes the complete result to the
append-only ledger before returning. Full workflow wiring remains step 2.6.

`three-way-match@v1` aligns invoice rows with PO rows by one-based position and
compares descriptions after Unicode NFKC normalization, case folding, and
whitespace collapse. It checks the PO number, vendor name, currency, and row
count. It records signed `actual − expected` Decimal deltas for:

- Invoice unit price against PO unit price (`equal`).
- Invoice quantity against ordered and cumulatively received quantities
  (`at_most`). This allows partial invoices up to the received quantity.
- Cumulative received quantity against ordered quantity (`at_most`).
- Invoice line net amount against invoice quantity × PO unit price (`equal`).
- Invoice net subtotal against the PO total and received value (`at_most`),
  and received value against the PO total (`at_most`).

For equality, a delta matches when `abs(actual − expected) <= tolerance`.
For a ceiling, it matches when `actual − expected <= tolerance`; negative
quantities or amounts never match. The effective tolerance is the greater of
the configured absolute band and `abs(expected) × relative band`. Default
price, line, and amount bands are 0.01 absolute or 0.5% relative; quantity is
exact. Every check records the rule, expected and actual values, signed delta,
effective tolerance, and result, including checks within tolerance.

The aggregate is `FAIL` if any known comparison mismatches, `INCOMPLETE` if
the PO snapshot is missing or any value needed for a comparison is unknown,
and `PASS` otherwise. A definite mismatch takes precedence over unknowns.
The result pins the extraction, ERP snapshot, and policy fingerprints. A
missing PO has no fabricated numeric evidence. A closed or cancelled PO may
match numerically; the policy step decides whether its status permits payment.
The matcher does not classify exception codes; step 2.3 maps its evidence to
the exception taxonomy.

This version assumes no previously invoiced quantity and does not reconcile
multiple invoices against one PO. It uses row position because the extraction
schema has no SKU; reordered invoice rows fail the deterministic comparison.
Those constraints are explicit in the evidence and can be revised through a
new policy version. Bank-change checks and fuzzy vendor matching belong to
later taxonomy or policy work.
