"""Pure hello stubs: no extraction, approval, model call, or business decision."""

import logging
from dataclasses import dataclass
from typing import Protocol

from invoiceops_agent.graph.state import GraphState, StateUpdate

logger = logging.getLogger(__name__)


class HelloStep(Protocol):
    async def __call__(self, state: GraphState) -> StateUpdate: ...


async def hello_start(state: GraphState) -> StateUpdate:
    logger.info(
        "graph_node_completed node=hello_start workflow=hello-stubs run_id=%s trace_id=%s",
        state.run_id,
        state.trace_id,
    )
    return {"status": "running", "completed_nodes": ["hello_start"]}


async def hello_finish(state: GraphState) -> StateUpdate:
    logger.info(
        "graph_node_completed node=hello_finish workflow=hello-stubs run_id=%s trace_id=%s",
        state.run_id,
        state.trace_id,
    )
    return {"status": "completed", "completed_nodes": ["hello_start", "hello_finish"]}


@dataclass(frozen=True)
class HelloNodes:
    start: HelloStep = hello_start
    finish: HelloStep = hello_finish
