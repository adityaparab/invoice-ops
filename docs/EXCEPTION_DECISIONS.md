# Exception decisions and four-eyes review

`POST /v1/exceptions/{id}/decision` requires a persona bearer token and an
`Idempotency-Key`. The JSON body contains `action` (`APPROVE`, `RETURN`, or
`ESCALATE`), a nonblank `rationale`, an uppercase `reason_code`, and
`proposal_id` (`null` for an analyst proposal). The token determines the
actor identity; the caller cannot supply or impersonate another actor ID.

An analyst submits the first proposal. This creates an append-only decision
and `decision.proposed` HUMAN ledger event and marks the exception `IN_REVIEW`.
The response stage is `PENDING_SIGNOFF`. A manager supplies that decision ID
as `proposal_id` to independently sign the final action. The manager may keep
the proposed action or choose `RETURN`/`ESCALATE` instead. `APPROVE` requires
an analyst `APPROVE` proposal. This writes a second append-only decision with
`supersedes_id` pointing to the proposal and a `decision.accepted` ledger
event; the response stage is `QUEUED_FOR_RESUME`. Auditors cannot submit
decisions. A key replay with the same actor and canonical request returns the
same response; reuse for different contents returns 409. A second proposal or
signoff for the exception also returns 409.

The API keeps checkpoint credentials separate from its restricted database
role. Run the accepted decision ID with the workflow profile's one-shot worker:

```sh
docker compose run --rm invoice-worker invoiceops-review-run <decision_id>
```

The worker uses the existing direct LiteLLM URL/key/model-name environment
variables; a review resume does not issue a new model call. It verifies the
independent signoff, resumes the paused LangGraph checkpoint, and checks the
committed `review.recorded` and `workflow.archived` events before setting
the run `COMPLETED` and exception `RESOLVED` or `ESCALATED`. A repeated worker
call is safe after a completed settlement. If resume fails, the signed
decision stays queued and the worker can be retried. The endpoint returns
201 for both new and identical replay requests; errors use RFC 7807.
