"""Run one accepted invoice through the durable workflow worker."""

import argparse
import asyncio
import json
import sys
from uuid import UUID

from pydantic import ValidationError

from invoiceops_agent.graph.errors import GraphError
from invoiceops_agent.graph.runtime import invoice_runtime


async def run_invoice(run_id: UUID) -> dict[str, str]:
    async with invoice_runtime(run_id) as workflow:
        result = await workflow.run()
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
    except (GraphError, ValidationError, OSError) as error:
        sys.stderr.write(f"invoice_workflow_failed error_type={type(error).__name__}\n")
        raise SystemExit(1) from None
    sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
