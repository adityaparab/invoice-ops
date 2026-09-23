# Synthetic ERP fixture (step 2.1)

`invoiceops_agent.tools.erp_generator.generate_fixture` uses Faker 40.39.0 and a local
seeded random stream. Version `synthetic-erp@v1` defaults to seed `20260827` and a fixed
as-of date of 2026-08-27. It never reads the wall clock or a global random stream. UUIDs,
creation times, names, SKU descriptions, quantities, prices, order dates, statuses, and
receipts reproduce byte-for-byte for the same seed and pinned dependency set.

The default fixture contains 12 active vendors, two orders per vendor, and one receipt
for each partially received or closed order. The 24 orders split evenly across `OPEN`,
`PARTIALLY_RECEIVED`, `CLOSED`, and `CANCELLED`. Purchase order line amounts and totals
use `Decimal` and two fractional digits. Receipt lines refer to the corresponding PO
line and never exceed its ordered quantity. Vendor names and IDs carry an explicit
synthetic marker. Bank strings start with deliberately invalid `GB00` check digits,
so they cannot be used as payment instructions.

The committed [ground truth report](../eval/reports/synthetic-erp-v1.json) records each
PO's vendor identity, status, ordered total and quantities, received quantities,
and full-receipt flag. It also pins the SHA-256 of the full typed fixture. Reproduce
the report to a *new* path with:

```bash
uv run python -m eval.datasets.prepare_erp --output /tmp/synthetic-erp-check.json
cmp eval/reports/synthetic-erp-v1.json /tmp/synthetic-erp-check.json
```

To load the fixture through the restricted application role, run:

```bash
docker compose --profile tools run --rm seed
```

`INVOICEOPS_SEED` can select a different nonnegative 32-bit seed. The seed service uses
`INVOICEOPS_POSTGRES_DSN` and requires the migration service to complete first. A
transaction and advisory lock insert all three ERP tables together. An exact rerun
does nothing; partial or changed records fail without overwriting business data.
The seed service logs only version, seed, counts, digest, outcome, and elapsed time.

This fixture supplies factual ERP records for the next deterministic matcher step.
It does not inject invoice anomalies or define match-policy outcomes; those are
separate plan steps. The Faker-generated names and bank markers remain synthetic.
