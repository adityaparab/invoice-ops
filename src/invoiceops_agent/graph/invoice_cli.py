"""Run one accepted invoice through the durable workflow worker."""

import argparse
import asyncio
import json
import sys
from uuid import UUID

import httpx2

from invoiceops_agent.gateway_client.telemetry import MetricGatewayTelemetry
from invoiceops_agent.graph.retry import RetryConfig
from invoiceops_agent.graph.runtime import (
    InvoiceRuntimeSettings,
    invoice_runtime,
    runtime_connection,
)
from invoiceops_agent.graph.state import InvoiceGraphState
from invoiceops_agent.graph.worker import PostgresAttemptStore, RetryWorker
from invoiceops_agent.obs.metrics import metrics_session
from invoiceops_agent.obs.tracing import tracing_session


async def run_invoice(
    run_id: UUID, *, gateway_transport: httpx2.AsyncBaseTransport | None = None
) -> dict[str, str]:
    async with (
        tracing_session("invoiceops-worker"),
        metrics_session("invoiceops-worker") as metrics,
    ):
        settings = InvoiceRuntimeSettings()
        retry = RetryConfig()

        async def run_once(value: UUID) -> InvoiceGraphState:
            async with invoice_runtime(
                value,
                settings=settings,
                gateway_telemetry=MetricGatewayTelemetry(metrics),
                gateway_transport=gateway_transport,
            ) as workflow:
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
