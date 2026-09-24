"""Process one accepted exception decision with owner-isolated checkpoints."""

import argparse
import asyncio
import json
import sys
from uuid import UUID

from invoiceops_agent.graph.review_worker import DecisionResumeWorker
from invoiceops_agent.graph.runtime import InvoiceRuntimeSettings


def main() -> None:
    parser = argparse.ArgumentParser(description="Resume one signed InvoiceOps review decision")
    parser.add_argument("decision_id", type=UUID)
    args = parser.parse_args()
    try:
        result = asyncio.run(
            DecisionResumeWorker(InvoiceRuntimeSettings()).process(args.decision_id)
        )
    except Exception as error:
        sys.stderr.write(f"review_workflow_failed error_type={type(error).__name__}\n")
        raise SystemExit(1) from None
    sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
