"""Node factories, prompt rendering and routing for the creative pipeline.

Every node is a closure over `PipelineDeps`, which makes the LLM call, prompts
and quota injectable. Tests pass a fake `llm`; production passes `call_llm`.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Type

from langgraph.graph import END

from ..blocklist import check_blocklist
from ..config import QuotaState, Settings
from ..llm import LLMError, call_llm
from ..schemas import AdCopy, AuditResult, CreativeBrief
from .state import (
    FAILED,
    MAX_ATTEMPTS,
    RUNNING,
    AWAITING_HUMAN,
    APPROVED,
    REJECTED,
    ESCALATED,
    HistoryEntry,
    PipelineState,
)

DEFAULT_PROMPT_DIR = Path(__file__).resolve().parents[2] / "prompts"

# Stage ids accepted by src.config.STAGES. The legal audit prompt is called
# with the configured "audit" stage but recorded under a clearer label.
LEGAL_STAGE = "audit"


def load_prompt(name: str, prompt_dir: Optional[Path] = None) -> str:
    """Read one system prompt from `prompts/<name>.md`."""
    path = Path(prompt_dir or DEFAULT_PROMPT_DIR) / f"{name}.md"
    return path.read_text(encoding="utf-8")


@dataclass
class PipelineDeps:
    """Everything a node needs from the outside world."""

    settings: Settings
    quota: QuotaState
    llm: Callable[..., Any] = call_llm
    prompt_dir: Path = DEFAULT_PROMPT_DIR
    trace_hook: Optional[Callable[[Any], None]] = None

    @classmethod
    def from_env(cls) -> "PipelineDeps":
        settings = Settings.from_env()
        return cls(settings=settings, quota=QuotaState(daily_quota=settings.daily_quota))


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _json(value: Any) -> str:
    if value is None:
        return "null"
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(value, indent=2, ensure_ascii=False)


def _ad_text(ad_copy: Optional[AdCopy]) -> str:
    if ad_copy is None:
        return ""
    parts = [ad_copy.headline, ad_copy.body, ad_copy.cta, *ad_copy.disclosures]
    return "\n".join(part for part in parts if part)


def _append(state: PipelineState, entry: HistoryEntry) -> List[HistoryEntry]:
    return state.history + [entry]


def _entry(
    deps: PipelineDeps,
    stage: str,
    started: float,
    attempt: int,
    status: str = "ok",
    detail: Optional[str] = None,
    model_stage: Optional[str] = None,
) -> HistoryEntry:
    try:
        model = deps.settings.model_for_stage(model_stage or stage)
    except KeyError:
        model = None
    return HistoryEntry(
        stage=stage,
        model=model or None,
        latency_ms=(time.perf_counter() - started) * 1000.0,
        attempt=attempt,
        status=status,
        detail=detail,
    )


def _failure(
    state: PipelineState,
    deps: PipelineDeps,
    stage: str,
    started: float,
    exc: Exception,
    attempt: Optional[int] = None,
    model_stage: Optional[str] = None,
) -> Dict[str, Any]:
    message = f"{type(exc).__name__}: {exc}"
    entry = _entry(
        deps,
        stage,
        started,
        state.attempt if attempt is None else attempt,
        status="error",
        detail=message,
        model_stage=model_stage,
    )
    return {"status": FAILED, "error": message, "history": _append(state, entry)}


def _call(
    deps: PipelineDeps,
    llm_stage: str,
    prompt_name: str,
    user: str,
    response_model: Type[Any],
) -> Any:
    """Call the injected LLM and coerce its result to `response_model`."""
    system = load_prompt(prompt_name, deps.prompt_dir)
    result = deps.llm(
        llm_stage,
        system,
        user,
        json_mode=True,
        response_model=response_model,
        settings=deps.settings,
        quota=deps.quota,
        trace_hook=deps.trace_hook,
    )
    if isinstance(result, response_model):
        return result
    return response_model.model_validate(result)


def _enforce_blocklist(audit: AuditResult, hits: List[str]) -> AuditResult:
    """Make the hard blocklist override any model verdict.

    The deterministic floor always wins: a blocklisted term can never be
    approved by an audit, no matter what the model returned.
    """
    if not hits:
        return audit
    notes = list(audit.required_edits)
    for hit in hits:
        note = f"Remove the blocked term: {hit}"
        if note not in notes:
            notes.append(note)
    return audit.model_copy(
        update={
            "approved": False,
            "required_edits": notes,
            "blocklist_hits": sorted(set(hits)),
        }
    )


def _creative_user(state: PipelineState) -> str:
    lines = ["Brief:", _json(state.brief), ""]
    if state.refine_notes:
        lines.append("Address every one of these required edits:")
        lines.extend(f"- {note}" for note in state.refine_notes)
        lines.append("")
    if state.ad_copy is not None:
        lines.append("Previous ad copy (do not repeat its problems):")
        lines.append(_json(state.ad_copy))
        lines.append("")
    lines.append("Return one improved AdCopy as JSON.")
    return "\n".join(lines)


def _audit_user(state: PipelineState, instruction: str) -> str:
    return "\n".join(
        [
            "Ad copy:",
            _json(state.ad_copy),
            "",
            "Brief:",
            _json(state.brief),
            "",
            instruction,
        ]
    )


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------

def make_intake_node(deps: PipelineDeps):
    def intake(state: PipelineState) -> Dict[str, Any]:
        if state.status == FAILED:
            return {}
        started = time.perf_counter()
        try:
            brief = _call(
                deps,
                "intake",
                "intake",
                f"Brief:\n{state.raw_input}",
                CreativeBrief,
            )
        except Exception as exc:  # LLMError, provider/quota and validation
            return _failure(state, deps, "intake", started, exc)
        if brief.raw_text is None:
            brief = brief.model_copy(update={"raw_text": state.raw_input})
        return {
            "brief": brief,
            "status": RUNNING,
            "history": _append(state, _entry(deps, "intake", started, state.attempt)),
        }

    return intake


def make_creative_node(deps: PipelineDeps):
    def creative(state: PipelineState) -> Dict[str, Any]:
        if state.status == FAILED:
            return {}
        attempt = state.attempt + 1
        started = time.perf_counter()
        try:
            ad_copy = _call(
                deps,
                "creative",
                "creative",
                _creative_user(state),
                AdCopy,
            )
        except Exception as exc:
            return _failure(state, deps, "creative", started, exc, attempt=attempt)
        return {
            "ad_copy": ad_copy,
            "attempt": attempt,
            "legal_result": None,
            "brand_result": None,
            "blocklist_hits": [],
            "human_decision": None,
            "status": RUNNING,
            "history": _append(
                state, _entry(deps, "creative", started, attempt)
            ),
        }

    return creative


def make_blocklist_node(deps: PipelineDeps):
    def blocklist(state: PipelineState) -> Dict[str, Any]:
        if state.status == FAILED:
            return {}
        started = time.perf_counter()
        hits = check_blocklist(_ad_text(state.ad_copy))
        detail = ", ".join(hits) if hits else "clean"
        return {
            "blocklist_hits": hits,
            "history": _append(
                state,
                _entry(deps, "blocklist", started, state.attempt, detail=detail),
            ),
        }

    return blocklist


def make_legal_audit_node(deps: PipelineDeps):
    def legal_audit(state: PipelineState) -> Dict[str, Any]:
        if state.status == FAILED:
            return {}
        started = time.perf_counter()
        try:
            audit = _call(
                deps,
                LEGAL_STAGE,
                "legal_audit",
                _audit_user(state, "Audit this ad copy."),
                AuditResult,
            )
        except Exception as exc:
            return _failure(
                state, deps, "legal_audit", started, exc, model_stage=LEGAL_STAGE
            )
        hits = sorted(set(check_blocklist(_ad_text(state.ad_copy))) | set(state.blocklist_hits))
        audit = _enforce_blocklist(audit, hits)
        return {
            "legal_result": audit,
            "blocklist_hits": hits,
            "history": _append(
                state,
                _entry(
                    deps,
                    "legal_audit",
                    started,
                    state.attempt,
                    detail="approved" if audit.approved else "rejected",
                    model_stage=LEGAL_STAGE,
                ),
            ),
        }

    return legal_audit


def make_brand_audit_node(deps: PipelineDeps):
    def brand_audit(state: PipelineState) -> Dict[str, Any]:
        if state.status == FAILED:
            return {}
        started = time.perf_counter()
        try:
            audit = _call(
                deps,
                "brand_audit",
                "brand_audit",
                _audit_user(state, "Check this ad copy against the brief."),
                AuditResult,
            )
        except Exception as exc:
            return _failure(state, deps, "brand_audit", started, exc)
        hits = sorted(set(check_blocklist(_ad_text(state.ad_copy))) | set(state.blocklist_hits))
        audit = _enforce_blocklist(audit, hits)
        return {
            "brand_result": audit,
            "blocklist_hits": hits,
            "history": _append(
                state,
                _entry(
                    deps,
                    "brand_audit",
                    started,
                    state.attempt,
                    detail="approved" if audit.approved else "rejected",
                ),
            ),
        }

    return brand_audit


def make_revise_node(deps: PipelineDeps):
    def revise(state: PipelineState) -> Dict[str, Any]:
        if state.status == FAILED:
            return {}
        started = time.perf_counter()
        notes: List[str] = []
        for audit in (state.legal_result, state.brand_result):
            if audit is not None:
                notes.extend(audit.required_edits)
        for hit in state.blocklist_hits:
            notes.append(f"Remove the blocked term: {hit}")
        if not notes:
            notes.append("Address the audit findings and produce a safer, on-brand rewrite.")

        deduped: List[str] = []
        for note in notes:
            note = (note or "").strip()
            if note and note not in deduped:
                deduped.append(note)

        return {
            "refine_notes": deduped,
            "history": _append(
                state,
                _entry(
                    deps,
                    "revise",
                    started,
                    state.attempt,
                    detail=f"{len(deduped)} note(s)",
                ),
            ),
        }

    return revise


def make_request_human_node(deps: PipelineDeps):
    def request_human(state: PipelineState) -> Dict[str, Any]:
        if state.status == FAILED:
            return {}
        started = time.perf_counter()
        return {
            "status": AWAITING_HUMAN,
            "history": _append(
                state, _entry(deps, "request_human", started, state.attempt)
            ),
        }

    return request_human


def _human_payload(state: PipelineState) -> Dict[str, Any]:
    return {
        "brief": state.brief.model_dump(mode="json") if state.brief else None,
        "ad_copy": state.ad_copy.model_dump(mode="json") if state.ad_copy else None,
        "legal_audit": (
            state.legal_result.model_dump(mode="json") if state.legal_result else None
        ),
        "brand_audit": (
            state.brand_result.model_dump(mode="json") if state.brand_result else None
        ),
        "blocklist_hits": list(state.blocklist_hits),
        "attempt": state.attempt,
        "instruction": (
            "Resume with {'decision': 'approve'|'edit'|'reject', "
            "'edited_copy': <AdCopy>}. 'edit' re-runs the full audit."
        ),
    }


def make_human_gate_node(deps: PipelineDeps):
    from langgraph.types import interrupt

    def human_gate(state: PipelineState) -> Dict[str, Any]:
        # Keep interrupt() outside any try/except: it suspends the graph by
        # raising internally, and swallowing that would break the pause.
        payload = interrupt(_human_payload(state))
        started = time.perf_counter()

        if not isinstance(payload, dict):
            return _invalid_decision(state, deps, started, payload)

        decision = payload.get("decision")
        if decision == "approve":
            return {
                "status": APPROVED,
                "human_decision": "approve",
                "history": _append(
                    state,
                    _entry(deps, "human_gate", started, state.attempt, detail="approve"),
                ),
            }
        if decision == "reject":
            return {
                "status": REJECTED,
                "human_decision": "reject",
                "history": _append(
                    state,
                    _entry(deps, "human_gate", started, state.attempt, detail="reject"),
                ),
            }
        if decision == "edit":
            try:
                edited = payload.get("edited_copy")
                if isinstance(edited, AdCopy):
                    ad_copy = edited
                else:
                    ad_copy = AdCopy.model_validate(edited)
            except Exception as exc:
                return _invalid_decision(state, deps, started, payload, exc)
            return {
                "ad_copy": ad_copy,
                "human_decision": "edit",
                "status": RUNNING,
                "legal_result": None,
                "brand_result": None,
                "blocklist_hits": [],
                "refine_notes": [],
                "attempt": 0,
                "history": _append(
                    state,
                    _entry(deps, "human_gate", started, state.attempt, detail="edit"),
                ),
            }
        return _invalid_decision(state, deps, started, payload)

    return human_gate


def _invalid_decision(
    state: PipelineState,
    deps: PipelineDeps,
    started: float,
    payload: Any,
    exc: Optional[Exception] = None,
) -> Dict[str, Any]:
    if isinstance(payload, dict):
        decision = payload.get("decision")
        message = f"Unknown human decision {decision!r}"
    else:
        message = f"Human decision payload must be a dict, got {type(payload).__name__}"
    if exc is not None:
        message = f"Invalid edited_copy: {type(exc).__name__}: {exc}"
    entry = _entry(
        deps, "human_gate", started, state.attempt, status="error", detail=message
    )
    return {"status": FAILED, "error": message, "history": _append(state, entry)}


def make_escalate_node(deps: PipelineDeps):
    def escalate(state: PipelineState) -> Dict[str, Any]:
        started = time.perf_counter()
        detail = f"no approval after {state.attempt} attempt(s)"
        return {
            "status": ESCALATED,
            "history": _append(
                state,
                _entry(deps, "escalate", started, state.attempt, detail=detail),
            ),
        }

    return escalate


# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------

def route_after_audits(state: PipelineState) -> str:
    if state.status == FAILED:
        return END
    if state.legal_result is None or state.brand_result is None:
        return "escalate"
    if state.legal_result.approved and state.brand_result.approved:
        return "request_human"
    if state.attempt >= MAX_ATTEMPTS:
        return "escalate"
    return "revise"


def route_after_human(state: PipelineState) -> str:
    if state.status == FAILED:
        return END
    if state.human_decision == "edit" and state.status == RUNNING:
        return "blocklist"
    return END
