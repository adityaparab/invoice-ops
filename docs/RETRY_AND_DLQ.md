# Invoice retry and dead-letter operation

The one-shot invoice worker records a run as `RUNNING` before graph execution and `COMPLETED` or
`PAUSED` after a settled graph result. It retries an uncaught infrastructure failure up to three
times with deterministic exponential delays of 0.5 and 1 second. The versioned
`invoice-retry@v1` policy caps every delay at 5 seconds and treats a `RUNNING` lease older than 240
seconds as recoverable after a process crash. The graph's synchronous checkpoint and committed
ledger evidence make recovery safe. Gateway calls already have their own bounded transport retry;
valid business findings and review outcomes never enter the worker retry loop.

Only typed connection, timeout, checkpoint, document availability, gateway availability, and ledger
storage failures are retryable. Validation failures, policy blocks, malformed evidence, and other
business or contract failures receive one worker attempt. A terminal failure updates the existing
`runs` row to `FAILED` with a sanitized error class, attempt count, retryable flag, and cycle number.
That indexed `FAILED` subset is the durable dead-letter queue. The status update and
`workflow.dead_lettered` ledger event commit in one transaction; raw exception messages and secrets
are never stored there. If the operational database is unavailable, a worker cannot record a dead
letter until connectivity returns; the expired `RUNNING` lease permits a later invocation to resume.

Inspect failed runs:

```bash
docker compose run --rm invoice-worker invoiceops-invoice-dlq list
```

After reviewing the failure, redrive a run with a unique key, operator identity, and reason, then
invoke the worker for that run ID:

```bash
docker compose run --rm invoice-worker invoiceops-invoice-dlq redrive <run_id> \
  --idempotency-key <unique-key> --actor-id <operator-id> --reason '<reviewed reason>'
docker compose run --rm invoice-worker invoiceops-invoice-run <run_id>
```

Redrive resets the per-cycle retry budget and appends `workflow.redriven` in the same transaction.
Repeating the exact key and request is a no-op; reusing the key with different actor or reason is
rejected. Ledger history preserves each failure and redrive cycle. These commands use only the
configured restricted application database connection and the existing worker settings.
