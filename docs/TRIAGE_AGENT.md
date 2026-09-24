# Triage recommendation agent

The invoice workflow calls `triage-reasoner` only after the deterministic
taxonomy, three-way match, validation, policy, and confidence gate route an
invoice to human review. A pure gatherer converts those results into bounded
facts with stable references, such as `taxonomy:DUP_NEAR`, `policy:status`, and
`match:numeric:3`. It excludes raw invoice text and bank values. The input
fingerprint and `triage-evidence@v1` version make the package reproducible.

The agent sends those facts with the packaged `triage@v1` prompt through the
single gateway client. The output is a typed advisory draft containing an
action, summary, rationale, and exact evidence references. References outside
the supplied package are rejected. A proposed `APPROVE` when the authoritative
policy status is `BLOCK` is also rejected. Gateway failures produce a typed
fallback. In both cases, the queue recommendation remains `REVIEW`, and the
human decision flow continues. The model cannot change the exception codes,
priority, policy result, or final approval.

Set `LITELLM_TRIAGE_MODEL` to a model name exposed by the user's LiteLLM
endpoint; if empty, `LITELLM_MODEL` is used. The workflow reads only
`LITELLM_API_BASE`, `LITELLM_MASTER_KEY`, and the model-name variables already
listed in `.env.example`. It does not read a proxy configuration file.

The graph commits the exception row and `triage.prepared` AGENT ledger event in
one transaction. The ledger pins `triage@v1`, the selected model name, and the
`exception-queue@v1` policy version; its payload includes the evidence digest,
facts, draft or fallback reason, token counts, and gateway latency when known.
On replay, the committed payload is returned without another model call.
