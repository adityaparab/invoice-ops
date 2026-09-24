# Composite confidence gate

Step 2.7 implements [ADR 0003](../adr/0003-composite-confidence-gate.md) as a pure Decimal
decision. Its `composite-gate@v2` configuration contains weights of 0.50 for the minimum observed
field confidence, 0.30 for the match term, and 0.20 for the policy term. The weights must sum to one.
The initial threshold is 0.95. The complete configuration and its SHA-256 fingerprint are recorded
with each decision; changing a value changes the fingerprint.

`score = 0.50 × min(observed field confidence) + 0.30 × (1 − normalized match delta) + 0.20 × policy severity term`

An unknown field is excluded from the confidence minimum because deterministic validation and
policy decide whether missing evidence is consequential. Every present header and line field is
included. The match delta uses equality comparisons only: it is the largest absolute numeric
difference divided by the larger of the two compared magnitudes and the configured floor of 1.
Directional `at_most` comparisons remain enforced by matching and policy; a valid partial receipt
does not lose gate confidence because it is smaller than the ordered quantity. The delta is capped
at 1 and evaluated under a fixed 28-digit Decimal context. A missing snapshot, no equality
comparison, or unknown equality check gives a delta of 1.
The policy severity term is 1 for `AUTO_APPROVE_ELIGIBLE`, 0.5 for `REVIEW`, and 0 for `BLOCK`.

The score is compared at its full Decimal precision. An eligible invoice with `score == threshold`
routes to `AUTO_APPROVE`; below threshold it routes to
`REVIEW`. Any policy
status other than `AUTO_APPROVE_ELIGIBLE` routes to review regardless of score. An explicit
`INVOICEOPS_AUTO_APPROVAL_ENABLED=false` also routes to review. The gate audits its score, each
component, route, reason, extraction/match/policy fingerprints, and full configuration before the
graph branches. Historical `composite-gate@v1` and `provisional-gate@v1` audit results remain
readable for already started runs.

The default threshold is an initial policy value, not an empirically calibrated operating point.
The Phase 5 eval harness will sweep thresholds and record any subsequent tuning in the experiment
log.
