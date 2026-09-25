# InvoiceOps: what the evaluation changed

*Publication draft · 25 September 2026 · Synthetic data only*

A synthetic invoice in the [InvoiceOps demo](demo/invoiceops-demo.mp4) has a line-item unit
price of €58.30. Its purchase order says €53.30. The extraction agent reads the document;
deterministic matching finds the difference; the run pauses before approval. An analyst
proposes escalation, a separate procurement manager signs off, and an auditor can trace both
actions back through the run. That path is more useful to me than a screen that simply says
“AI approved an invoice.” It shows where a model helps and where it cannot make the decision.

This project began with a hypothesis: a model-assisted invoice workflow could automate clean
cases while preserving strict exception detection, human review, and an audit trail. The
implementation uses FastAPI, a durable LangGraph workflow, Postgres and MinIO, and a single
gateway client over the configured LiteLLM URL and key. Extraction and triage call models;
matching, policy, idempotency, and approval controls are deterministic. The
[experiment log](../eval/reports/experiment-log-v1.json) records how the measured results
changed the design.

## Build a test that can say no

The first extraction baseline used 32 prepared tier-A invoice images. Its micro field F1 was
0.7226, and one malformed model output escalated. That was useful development evidence,
not a release result: it said nothing about lower-quality tiers or the end-to-end decision.
The [baseline report](../eval/reports/extraction-baseline-v1.json) remains versioned rather
than being replaced by a later, better score.

The larger [`golden/v1.0.1` set](../docs/EVALUATION.md) contains 500 labeled invoices:
50 re-labeled public-source images for extraction only, 300 clean synthetic invoices with
matching ERP records, and 150 synthetic invoices with injected anomalies. One hundred cases
form the development split; 400 are held out. The set includes duplicate, price, quantity,
bank-detail, currency, tax, math, missing-PO, and stale-PO cases. Each label and synthetic
ERP record is pinned. The images and records contain no customer data.

The evaluation runner goes through the actual API, storage, worker, and audit readback.
It does not call graph nodes in isolation to claim an end-to-end result. The release method
requires three independent 500-case runs in fresh Compose stacks. It scores quality and
gateway-reported cost from the first complete run, and pools audited auto-approval latency
across all three. A recorded cassette smoke is valuable for CI reliability, but it cannot
stand in for live model-quality evidence.

## The first live pipeline result failed in useful ways

The first 100-case development run detected 96.67% of injected anomalies, but it escalated
every clean routing case. Field F1 was 0.9310, money-field F1 was 0.9571, routing accuracy
was 0.3000, and clean straight-through approval was zero. Cost and a three-run p95 were
unavailable. Three runs dead-lettered after repeated triage citation IDs. The diagnostic
report counted 48 near-duplicate false positives and 26 bank-change false positives;
bank-account OCR often lost a zero. Even clean cases that escaped those findings fell
below the 0.95 confidence cutoff. The [v0.1 report](../eval/reports/pipeline-eval-openai-prod-development-v01.json)
made the failure visible instead of letting a passing unit test hide it.

We corrected the citation handling, constrained similarity to invoice identity, and made
the confidence gate sensitive to the direction of a match difference. Extraction gained
bounded identifier OCR and more literal transcription of money and tax rates. The team also
corrected 50 Voxel51 gross-versus-net row labels in `golden/v1.0.1`; the documents, split,
anomaly assignments, and ERP records did not change. Prompts, OCR rules, similarity, and
gate policies were versioned. The extraction route also changed from `gpt4omini` to
`gpt5nano`, with `gpt4omini` retained for triage. These changes arrived as a package, so
the improvement cannot honestly be assigned to one prompt or one algorithm.

The next complete 100-case development run detected 30/30 anomalies, made 0/60 clean false
escalations, routed 90/90 eligible cases correctly, and auto-approved 60/60 clean cases.
Field F1 reached 0.9785 and money-field F1 0.9944. Gateway cost was observed for all
100 cases at $0.0012015817 per invoice. Its audited auto-approval p95 was 31.1 seconds,
but one run could not validate the release latency requirement. The
[v0.6 experiment entry](../eval/reports/experiment-log-v1.json) therefore authorized a
full-suite measurement, not a quality-gate activation.

## A latency regression changed retry behavior

The first complete 500-case run exposed a different failure. Seven observed quality and
cost measures met their floors, but a one-run auto-approval p95 was 53.2 seconds against a
45-second target. Nineteen clean cases needed two or three extraction attempts after a
30-second provider-attempt bound. We increased that bound to 40 seconds and prevented a
primary retry from consuming the time reserved for the named fallback. This was a bounded
retry change, not a relaxed latency target. A repeated 100-case development run showed
all 60 clean extractions using one gateway attempt and a one-run audited p95 of 32.4
seconds. The full suite still needed three fresh repetitions.

## The release baseline

Three independent 500-case runs on the corrected runtime completed without worker errors.
The first run found 149/150 injected anomalies, had 10/300 clean false escalations,
achieved field F1 0.9732 and money-field F1 0.9913, routed 440/450 ERP-backed cases
correctly, and auto-approved 290/300 clean cases. Gateway-reported cost was present for
500/500 invoices and averaged $0.00122339726. Across three runs, the audited
auto-approval p95 was 30.872508 seconds over 875 observed approvals. All eight unchanged
release floors passed. The [primary report](../eval/reports/golden-v1.0.1-openai-prod.json),
[diagnostics](../eval/reports/diagnostics-golden-v1.0.1-openai-prod.json), and
[CI comparator](../eval/ci_gate.py) make the evidence and gate rule inspectable.

The gate compares a committed, complete `openai-prod` report with the main-branch baseline.
It checks the eight floors, evidence coverage, model class, corpus identity, and regression
tolerance. CI does not make a new provider call on every pull request. That distinction
matters: a green deterministic build is not itself a fresh quality measurement.

## An ADK comparison with a causal limit

We then implemented the same invoice state machine with Google ADK function nodes, sharing
the application services, deterministic controls, LiteLLM gateway, and ledger. ADK sessions
use a separate Postgres schema; both variants have restart and human-resume tests. Three
fresh 500-case ADK runs used `gemini25flash` for extraction and triage and
`gemini-embedding` for similarity. The first had 150/150 anomaly recall, 1/300 clean false
escalations, field F1 0.9780, 449/450 correct routes, and 299/300 clean auto-approvals.
Pooled audited p95 was 7.713483 seconds over 895 approvals. The
[ADK report](../eval/reports/golden-v1.0.1-adk-gemini.json) records those results.

There are two hard limits on that comparison. First, LiteLLM returned zero cost for every
Gemini-route invoice. The report has 500/500 *proxy-value* coverage per run, but actual
provider spend is unverified. I cannot call it a cheaper route. Second, the ADK variant
also changed the model route. Its quality and latency differences describe the whole
variant; they do not show that ADK itself caused an improvement. The
[comparison decision](../adr/0011-langgraph-adk-comparison.md) keeps LangGraph as the
production default because its typed checkpoints and review API require less adaptation in
this application, while retaining ADK as an executable comparison.

## What is still unproven

The golden set is synthetic and concentrated in quality tier A; it cannot predict the error
rate on real vendor traffic. The one-minute audit-reconstruction goal has no timed result.
The versioned triage judge was not run for either published full-suite diagnostic report.
Provider-side Gemini spend needs independent reconciliation. A cleaner framework experiment
would hold the model routes constant while changing only orchestration, and fault-inject a
process crash inside an unfinished ADK node. None of those gaps is erased by a passing
table of metrics.

The useful outcome is a workflow that can be challenged: the failed runs remain in the
experiment record, the release baseline has explicit denominators, and a human can stop and
explain a consequential decision. Payment execution, vendor onboarding, and real ERP
writeback remain outside this project's scope.
