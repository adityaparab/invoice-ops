"""Durable invoice workflow topology with explicit decision and review branches."""

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from invoiceops_agent.graph.invoice_nodes import InvoiceNodes
from invoiceops_agent.graph.state import InvoiceGraphState
from invoiceops_agent.obs.tracing import traced_node

InvoiceGraph = CompiledStateGraph[InvoiceGraphState, None, InvoiceGraphState, InvoiceGraphState]


def build_invoice_graph(saver: BaseCheckpointSaver[str], nodes: InvoiceNodes) -> InvoiceGraph:
    builder = StateGraph(InvoiceGraphState)
    builder.add_node("Ingest", traced_node("Ingest", nodes.ingest))
    builder.add_node("Extract", traced_node("Extract", nodes.extract))
    builder.add_node("Validate", traced_node("Validate", nodes.validate))
    builder.add_node("Match3Way", traced_node("Match3Way", nodes.match_three_way))
    builder.add_node("Policy", traced_node("Policy", nodes.policy))
    builder.add_node("Gate", traced_node("Gate", nodes.gate))
    builder.add_node("AutoApprove", traced_node("AutoApprove", nodes.auto_approve))
    builder.add_node("ExceptionTriage", traced_node("ExceptionTriage", nodes.exception_triage))
    builder.add_node("HumanReview", traced_node("HumanReview", nodes.human_review))
    builder.add_node("Archive", traced_node("Archive", nodes.archive))
    builder.add_node("Reject", traced_node("Reject", nodes.reject))
    builder.add_edge(START, "Ingest")
    builder.add_conditional_edges(
        "Ingest", lambda state: "Reject" if state.duplicate else "Extract"
    )
    builder.add_conditional_edges(
        "Extract",
        lambda state: "Validate" if state.extraction is not None else "ExceptionTriage",
    )
    builder.add_edge("Validate", "Match3Way")
    builder.add_edge("Match3Way", "Policy")
    builder.add_edge("Policy", "Gate")
    builder.add_conditional_edges(
        "Gate", lambda state: "AutoApprove" if state.route == "AUTO_APPROVE" else "ExceptionTriage"
    )
    builder.add_edge("AutoApprove", "Archive")
    builder.add_edge("ExceptionTriage", "HumanReview")
    builder.add_edge("HumanReview", "Archive")
    builder.add_edge("Archive", END)
    builder.add_edge("Reject", END)
    return builder.compile(checkpointer=saver, name="invoiceops-invoice-v1")
