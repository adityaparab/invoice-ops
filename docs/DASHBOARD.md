# Manager dashboard

`GET /v1/dashboard?period_days=30` accepts only the procurement manager's
`INVOICEOPS_MANAGER_TOKEN`. The period is 1–90 UTC calendar days including
today. The response is one repeatable-read snapshot of invoices created in
that period, latest exceptions, and their append-only ledger evidence. Other
roles receive 403; missing or invalid tokens receive 401. Errors use RFC 7807.

`invoice_count` is the number of invoices received in the period.
`resolved_count` includes `APPROVED`, `REJECTED`, `RETURNED`, and `ARCHIVED`.
`auto_approved_count` counts resolved invoices with an
`approval.auto_granted` ledger event. `stp_rate` is the exact Decimal ratio
`auto_approved_count / resolved_count`; it is `null` when no invoice has
resolved. Daily volume includes zero-count UTC days.

The exception breakdown uses the latest exception per invoice. Open aging
includes every status except `RESOLVED`, measured from exception creation at
the response's `as_of` time. `sla_overdue` counts open exceptions whose SLA
deadline has passed.

Observed cost sums nonnegative `cost_usd` values from extraction invocation
records, embedding gateway evidence in `similarity.completed`, and the triage
result in the ledger. `cost_observed_invoices` counts
invoices with at least one reported cost. The per-invoice figure divides the
observed total by that observed count; it is `null` when none have reported
cost. `cost_coverage` is `COMPLETE` only when every invoice in the period has
reported cost, `PARTIAL` when some have, and `UNAVAILABLE` otherwise. Missing
cost is never silently treated as zero. All money and ratio values are
serialized as decimal strings.

The React dashboard shows these figures, Recharts volume/aging/type charts,
and a link to the operational review queue. Browser charts never compute
authoritative totals from paginated queue rows.
