"""Wire the creative pipeline into a LangGraph state machine."""
from __future__ import annotations

import uuid
from typing import Any, Dict, Optional

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from .nodes import (
    PipelineDeps,
    make_blocklist_node,
    make_brand_audit_node,
    make_creative_node,
    make_escalate_node,
    make_human_gate_node,
    make_intake_node,
    make_legal_audit_node,
    make_request_human_node,
    make_revise_node,
    route_after_audits,
    route_after_human,
)
from .state import PipelineState

__all__ = ["build_graph", "run_pipeline", "new_thread_config", "MAX_ATTEMPTS"]

from .state import MAX_ATTEMPTS  # re-exported for callers


def build_graph(deps: Optional[PipelineDeps] = None, checkpointer: Any = None):
    """Compile the pipeline.

    A checkpointer is required because the human gate uses `interrupt()`.
    `InMemorySaver` is the safe default for tests and local demos.
    """
    deps = deps or PipelineDeps.from_env()
    if checkpointer is None:
        checkpointer = InMemorySaver()

    graph = StateGraph(PipelineState)
    graph.add_node("intake", make_intake_node(deps))
    graph.add_node("creative", make_creative_node(deps))
    graph.add_node("blocklist", make_blocklist_node(deps))
    graph.add_node("legal_audit", make_legal_audit_node(deps))
    graph.add_node("brand_audit", make_brand_audit_node(deps))
    graph.add_node("revise", make_revise_node(deps))
    graph.add_node("request_human", make_request_human_node(deps))
    graph.add_node("human_gate", make_human_gate_node(deps))
    graph.add_node("escalate", make_escalate_node(deps))

    graph.add_edge(START, "intake")
    graph.add_edge("intake", "creative")
    graph.add_edge("creative", "blocklist")
    graph.add_edge("blocklist", "legal_audit")
    graph.add_edge("legal_audit", "brand_audit")
    graph.add_conditional_edges(
        "brand_audit",
        route_after_audits,
        {
            "revise": "revise",
            "request_human": "request_human",
            "escalate": "escalate",
            END: END,
        },
    )
    graph.add_edge("revise", "creative")
    graph.add_edge("request_human", "human_gate")
    graph.add_conditional_edges(
        "human_gate",
        route_after_human,
        {"blocklist": "blocklist", END: END},
    )
    graph.add_edge("escalate", END)

    return graph.compile(checkpointer=checkpointer)


def new_thread_config(thread_id: Optional[str] = None) -> Dict[str, Any]:
    return {"configurable": {"thread_id": thread_id or uuid.uuid4().hex}}


def run_pipeline(
    raw_input: str,
    deps: Optional[PipelineDeps] = None,
    thread_id: Optional[str] = None,
    checkpointer: Any = None,
) -> Dict[str, Any]:
    """Run until the human gate or a terminal state.

    Returns the graph state. When a human decision is required the result
    contains an `__interrupt__` key; resume with
    `app.invoke(Command(resume={...}), config=config)` on the same thread.
    """
    app = build_graph(deps=deps, checkpointer=checkpointer)
    return app.invoke({"raw_input": raw_input}, new_thread_config(thread_id))
