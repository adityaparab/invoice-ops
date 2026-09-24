"""CLI for the persisted hello-stub workflow, emitting structured logs only."""

import argparse
import asyncio
import logging
from uuid import UUID, uuid4

from pydantic import ValidationError

from invoiceops_agent.graph.checkpoints import postgres_graph
from invoiceops_agent.graph.errors import GraphError
from invoiceops_agent.graph.settings import GraphSettings
from invoiceops_agent.obs.logging import configure_logging
from invoiceops_agent.obs.tracing import tracing_session

logger = logging.getLogger(__name__)


async def run_demo(*, run_id: UUID, invoice_id: UUID) -> int:
    trace_id = uuid4().hex
    try:
        async with tracing_session("invoiceops-graph-demo"):
            settings = GraphSettings()
            async with postgres_graph(settings) as runner:
                result = await runner.run(run_id=run_id, invoice_id=invoice_id, trace_id=trace_id)
    except (GraphError, ValidationError) as error:
        logger.error(
            "graph_demo_failed run_id=%s trace_id=%s error_type=%s",
            run_id,
            trace_id,
            type(error).__name__,
        )
        return 1
    logger.info("graph_demo_result state=%s", result.model_dump_json())
    return 0


def main() -> int:
    configure_logging()
    parser = argparse.ArgumentParser(description="Run persisted InvoiceOps hello stubs")
    parser.add_argument("--run-id", type=UUID, default=None)
    parser.add_argument("--invoice-id", type=UUID, default=None)
    arguments = parser.parse_args()
    return asyncio.run(
        run_demo(run_id=arguments.run_id or uuid4(), invoice_id=arguments.invoice_id or uuid4())
    )
