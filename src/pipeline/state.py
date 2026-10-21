"""State shared by every node in the creative pipeline."""
from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field

from ..schemas import AdCopy, AuditResult, CreativeBrief

# Terminal and intermediate pipeline statuses.
RUNNING = "running"
AWAITING_HUMAN = "awaiting_human"
APPROVED = "approved"
REJECTED = "rejected"
ESCALATED = "escalated_to_human"
FAILED = "failed"

# Hard cap on automatic rewrite cycles. After this many creative attempts the
# pipeline stops rewriting and escalates to a person (project rule 6).
MAX_ATTEMPTS = 3


class HistoryEntry(BaseModel):
    """One auditable step: which stage ran, with what result and latency."""

    stage: str
    model: Optional[str] = None
    latency_ms: float = 0.0
    attempt: int = 0
    status: str = "ok"
    detail: Optional[str] = None


class PipelineState(BaseModel):
    """Everything the graph needs, serialised by the checkpointer.

    Nodes return partial dicts; LangGraph merges them into this model. Lists
    are plain last-value-wins fields and nodes append explicitly, so the state
    stays deterministic and easy to test.
    """

    raw_input: str
    brief: Optional[CreativeBrief] = None
    ad_copy: Optional[AdCopy] = None
    blocklist_hits: List[str] = Field(default_factory=list)
    legal_result: Optional[AuditResult] = None
    brand_result: Optional[AuditResult] = None
    attempt: int = 0
    history: List[HistoryEntry] = Field(default_factory=list)
    status: str = RUNNING
    human_decision: Optional[str] = None
    refine_notes: List[str] = Field(default_factory=list)
    error: Optional[str] = None
