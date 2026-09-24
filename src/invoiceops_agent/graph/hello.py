"""Two-node hello topology with a checkpoint boundary after each stub."""

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from invoiceops_agent.graph.nodes.hello import HelloNodes
from invoiceops_agent.graph.state import GraphState
from invoiceops_agent.obs.tracing import traced_node

HelloGraph = CompiledStateGraph[GraphState, None, GraphState, GraphState]


def build_hello_graph(
    saver: BaseCheckpointSaver[str], *, nodes: HelloNodes | None = None
) -> HelloGraph:
    steps = nodes if nodes is not None else HelloNodes()
    builder = StateGraph(GraphState)
    builder.add_node("hello_start", traced_node("hello_start", steps.start))
    builder.add_node("hello_finish", traced_node("hello_finish", steps.finish))
    builder.add_edge(START, "hello_start")
    builder.add_edge("hello_start", "hello_finish")
    builder.add_edge("hello_finish", END)
    return builder.compile(checkpointer=saver, name="invoiceops-hello-v1")
