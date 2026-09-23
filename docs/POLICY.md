# Deterministic invoice policy v1

Step 2.5 evaluates a typed extraction, validation result, three-way match,
exception taxonomy, and optional ERP snapshot. Their fingerprints must agree.
The caller supplies `as_of`; the pure policy function never reads the clock,
performs I/O, or calls a model. The result pins the input and policy hashes and
is appended to the ledger as `policy.completed` before it is returned.

The example policy is `invoice-policy@v1`. Amounts are compared only with a
band for the invoice's currency; there is no implicit exchange-rate conversion.
These are synthetic example limits, not a jurisdictional or bank policy.

| Currency | Auto eligible through | AP manager through | Procurement director through | Hard cap |
| --- | ---: | ---: | ---: | ---: |
| EUR, GBP, USD | 10,000 | 50,000 | 100,000 | 250,000 |
| JPY | 1,000,000 | 5,000,000 | 10,000,000 | 25,000,000 |
| KWD | 3,000 | 15,000 | 30,000 | 75,000 |
| PLN | 40,000 | 200,000 | 400,000 | 1,000,000 |

Upper bounds are inclusive. A configured manager, director, or dual-control tier
means `APPROVAL_REQUIRED` and routes to review. An amount over its hard cap is
blocked. Missing, negative, or unsupported-currency amounts cannot become auto
eligible. The result records both its required approval tier and its routing
status.

An exact duplicate or cancelled PO is blocked. A closed PO is reviewed. An open
or partially received PO older than 365 days is reviewed; age exactly 365 days
is allowed. A PO issued after `as_of` is reviewed. Any taxonomy exception or
unresolved evidence, failed validation, or nonpassing match also routes to
review. `AUTO_APPROVE_ELIGIBLE` requires none of these findings and is only
eligibility evidence for the later confidence gate and graph routing. It does
not execute an approval or payment.

The approval bands and maximum PO age are immutable versioned configuration.
Changing them requires a new reviewed policy version. The full graph consumes
this standalone policy node in step 2.6.
