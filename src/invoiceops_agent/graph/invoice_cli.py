"""Run one accepted invoice through the durable workflow worker."""

import argparse
import asyncio
import json
import sys
from uuid import UUID

from invoiceops_agent.graph.retry import RetryConfig
from invoiceops_agent.graph.runtime import (
    InvoiceRuntimeSettings,
    invoice_runtime,
    runtime_connection,
)
from invoiceops_agent.graph.state import InvoiceGraphState
from invoiceops_agent.graph.worker import PostgresAttemptStore, RetryWorker


async def run_invoice(run_id: UUID) -> dict[str, str]:
    settings = InvoiceRuntimeSettings()
    retry = RetryConfig()

    async def run_once(value: UUID) -> InvoiceGraphState:
        async with invoice_runtime(value, settings=settings) as workflow:
            return await workflow.run()

    store = PostgresAttemptStore(
        lambda: runtime_connection(settings.postgres_dsn.get_secret_value()), retry
    )
    result = await RetryWorker(store, run_once, retry).process(run_id)
    return {
        "run_id": str(result.run_id),
        "invoice_id": str(result.invoice_id),
        "status": result.status,
        "route": result.route or "NONE",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one accepted invoice through InvoiceOps")
    parser.add_argument("run_id", type=UUID)
    args = parser.parse_args()
    try:
        result = asyncio.run(run_invoice(args.run_id))
    except Exception as error:
        sys.stderr.write(f"invoice_workflow_failed error_type={type(error).__name__}\n")
        raise SystemExit(1) from None
    sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
