"""LangGraph pipeline tests. Fully mocked: no network, no cost.

Every LLM call is replaced by `FakeLLM`, so the graph wiring, routing,
blocklist floor and human gate are exercised deterministically.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END
from langgraph.types import Command

from src.config import QuotaState, Settings
from src.llm import LLMError, QuotaExceeded
from src.schemas import AdCopy, AuditResult, CreativeBrief
from src.pipeline import (
    APPROVED,
    AWAITING_HUMAN,
    ESCALATED,
    FAILED,
    MAX_ATTEMPTS,
    REJECTED,
    RUNNING,
    PipelineDeps,
    PipelineState,
    build_graph,
    load_prompt,
    new_thread_config,
    run_pipeline,
)
from src.pipeline.nodes import (
    DEFAULT_PROMPT_DIR,
    LEGAL_STAGE,
    _enforce_blocklist,
    make_blocklist_node,
    make_brand_audit_node,
    make_creative_node,
    make_escalate_node,
    make_intake_node,
    make_legal_audit_node,
    make_request_human_node,
    make_revise_node,
    route_after_audits,
    route_after_human,
)

# InMemorySaver round-trips pydantic models by class name; LangGraph warns
# about that until a message-pack allowlist is registered. Harmless here.
pytestmark = pytest.mark.filterwarnings(
    "ignore", message="Deserializing unregistered type"
)


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

BRIEF = CreativeBrief(client="Acme", product="Coffee", tone="warm")
GOOD = AdCopy(
    headline="Fresh roast",
    body="Small batch coffee.",
    cta="Order now",
    disclosures=["Ships in 2 days"],
)
BAD = AdCopy(headline="Miracle cure", body="Guaranteed results.", cta="Buy now")
EDITED = AdCopy(
    headline="Edited headline", body="Edited body.", cta="Edited CTA"
)
APPROVED_AUDIT = AuditResult(approved=True, risk_score=2)
REJECTED_AUDIT = AuditResult(
    approved=False, risk_score=8, required_edits=["Say less"], reasons=["Too pushy"]
)
NO_EDIT_REJECT = AuditResult(approved=False, risk_score=5)


class Call:
    def __init__(self, stage, system, user, kwargs):
        self.stage = stage
        self.system = system
        self.user = user
        self.kwargs = kwargs


def _take(queue):
    if not queue:
        return None
    if len(queue) == 1:
        return queue[0]
    return queue.pop(0)


class FakeLLM:
    """Deterministic stand-in for `src.llm.call_llm`.

    Queues with one item are reused (stable answer); longer queues are
    consumed in order, so multi-attempt flows can change their output.
    """

    def __init__(
        self,
        *,
        briefs=(BRIEF,),
        copies=(GOOD,),
        legal=(APPROVED_AUDIT,),
        brand=(APPROVED_AUDIT,),
        error=None,
        error_stage=None,
    ):
        self._briefs = list(briefs)
        self._copies = list(copies)
        self._legal = list(legal)
        self._brand = list(brand)
        self.error = error
        self.error_stage = error_stage
        self.calls = []

    def __call__(self, stage, system, user, **kwargs):
        self.calls.append(Call(stage, system, user, kwargs))
        if self.error is not None and (
            self.error_stage is None or self.error_stage == stage
        ):
            raise self.error
        model = kwargs.get("response_model")
        if model is CreativeBrief:
            return _take(self._briefs)
        if model is AdCopy:
            return _take(self._copies)
        if model is AuditResult:
            return _take(self._legal if stage == LEGAL_STAGE else self._brand)
        raise AssertionError(f"unexpected response_model {model!r}")

    @property
    def stages(self):
        return [call.stage for call in self.calls]

    def calls_for(self, stage):
        return [call for call in self.calls if call.stage == stage]


def _settings():
    return Settings(
        api_key="test",
        model_intake="intake-model",
        model_creative="creative-model",
        model_audit="audit-model",
        model_brand_audit="brand-model",
        timeout_seconds=1.0,
        retry_backoff_base=0.0,
        daily_quota=100,
    )


def _deps(llm, prompt_dir=None):
    settings = _settings()
    return PipelineDeps(
        settings=settings,
        quota=QuotaState(daily_quota=settings.daily_quota),
        llm=llm,
        prompt_dir=Path(prompt_dir) if prompt_dir else DEFAULT_PROMPT_DIR,
    )


def _app(llm=None, **kwargs):
    return build_graph(deps=_deps(llm or FakeLLM()), **kwargs)


def _get(obj, key, default=None):
    """Read a field from either a dict or a pydantic model."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _stages_of(state):
    return [_get(entry, "stage") for entry in _get(state, "history", [])]


def _start(app, raw="A coffee brand brief", thread_id="t"):
    return app.invoke({"raw_input": raw}, new_thread_config(thread_id))


def _resume(app, payload, thread_id="t"):
    return app.invoke(Command(resume=payload), new_thread_config(thread_id))


def _interrupt_payload(result):
    item = result["__interrupt__"][0]
    if isinstance(item, dict):
        return item.get("value")
    return getattr(item, "value", None)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_happy_path_stage_order():
    llm = FakeLLM()
    app = _app(llm)
    result = _start(app)
    assert "__interrupt__" in result
    assert _get(result, "status") == AWAITING_HUMAN
    assert _stages_of(result) == [
        "intake",
        "creative",
        "blocklist",
        "legal_audit",
        "brand_audit",
        "request_human",
    ]
    assert _get(result, "attempt") == 1


def test_happy_path_history_models_and_latency():
    app = _app(FakeLLM())
    result = _start(app)
    expected = {
        "intake": "intake-model",
        "creative": "creative-model",
        "legal_audit": "audit-model",
        "brand_audit": "brand-model",
        "blocklist": None,
        "request_human": None,
    }
    for entry in result["history"]:
        assert _get(entry, "model") == expected[_get(entry, "stage")]
        assert _get(entry, "latency_ms") >= 0.0


def test_approve_returns_approved():
    app = _app(FakeLLM())
    _start(app)
    final = _resume(app, {"decision": "approve"})
    assert _get(final, "status") == APPROVED
    assert _get(final, "human_decision") == "approve"
    assert _get(final["ad_copy"], "headline") == "Fresh roast"


def test_reject_returns_rejected():
    app = _app(FakeLLM())
    _start(app)
    final = _resume(app, {"decision": "reject"})
    assert _get(final, "status") == REJECTED
    assert _get(final, "human_decision") == "reject"


def test_get_state_persists_awaiting_human():
    app = _app(FakeLLM())
    _start(app)
    state = app.get_state(new_thread_config("t"))
    assert _get(state.values, "status") == AWAITING_HUMAN


def test_intake_fills_raw_text_when_missing():
    llm = FakeLLM(briefs=(CreativeBrief(client="Acme", product="Coffee"),))
    result = _start(_app(llm), raw="the original brief text")
    assert _get(result["brief"], "raw_text") == "the original brief text"


def test_intake_preserves_given_raw_text():
    llm = FakeLLM(briefs=(CreativeBrief(client="Acme", raw_text="given"),))
    result = _start(_app(llm), raw="the original brief text")
    assert _get(result["brief"], "raw_text") == "given"


def test_creative_prompt_contains_brief_facts():
    llm = FakeLLM()
    _start(_app(llm))
    creative = llm.calls_for("creative")[0]
    assert "Acme" in creative.user
    assert "Coffee" in creative.user
    assert "Return one improved AdCopy as JSON." in creative.user


def test_every_call_uses_json_mode_and_injected_deps():
    llm = FakeLLM()
    _start(_app(llm))
    assert llm.calls
    for call in llm.calls:
        assert call.kwargs["json_mode"] is True
        assert call.kwargs["response_model"] is not None
        assert call.kwargs["settings"] is not None
        assert call.kwargs["quota"] is not None


def test_custom_prompt_dir_is_used(tmp_path):
    for name in ("intake", "creative", "legal_audit", "brand_audit"):
        (tmp_path / f"{name}.md").write_text(f"CUSTOM {name} PROMPT", encoding="utf-8")
    llm = FakeLLM()
    app = build_graph(deps=_deps(llm, prompt_dir=tmp_path))
    _start(app)
    assert llm.calls[0].system == "CUSTOM intake PROMPT"
    assert llm.calls_for("creative")[0].system == "CUSTOM creative PROMPT"


def test_load_prompt_reads_default_files():
    assert load_prompt("intake")
    assert load_prompt("creative")
    assert load_prompt("legal_audit")
    assert load_prompt("brand_audit")


def test_legal_stage_constant_matches_config():
    assert LEGAL_STAGE == "audit"


# ---------------------------------------------------------------------------
# Blocklist floor
# ---------------------------------------------------------------------------

def test_blocklist_rewrites_before_human_gate():
    llm = FakeLLM(copies=(BAD, GOOD))
    app = _app(llm)
    result = _start(app)
    assert "__interrupt__" in result
    assert _get(result, "status") == AWAITING_HUMAN
    assert _get(result, "attempt") == 2
    assert _get(result, "blocklist_hits") == []
    assert _get(result["legal_result"], "approved") is True
    assert "revise" in _stages_of(result)


def test_blocklist_node_never_calls_llm():
    llm = FakeLLM(copies=(BAD, GOOD))
    _start(_app(llm))
    assert "blocklist" not in llm.stages


def test_blocklist_note_reaches_revise_prompt():
    llm = FakeLLM(copies=(BAD, GOOD))
    _start(_app(llm))
    second_creative = llm.calls_for("creative")[1]
    assert "Remove the blocked term: miracle" in second_creative.user


def test_blocklist_history_detail_names_terms():
    llm = FakeLLM(copies=(BAD, GOOD))
    result = _start(_app(llm))
    details = [
        _get(entry, "detail")
        for entry in result["history"]
        if _get(entry, "stage") == "blocklist"
    ]
    assert details[0]
    assert "miracle" in details[0]


def test_model_approval_cannot_override_blocklist():
    # Audits always approve, but the copy is blocklisted every time.
    llm = FakeLLM(copies=(BAD, BAD, BAD))
    result = _start(_app(llm))
    assert "__interrupt__" not in result
    assert _get(result, "status") == ESCALATED
    assert _get(result["legal_result"], "approved") is False
    assert _get(result["legal_result"], "blocklist_hits")


def test_enforce_blocklist_overrides_and_sorts():
    out = _enforce_blocklist(APPROVED_AUDIT, ["miracle", "cure"])
    assert out.approved is False
    assert out.blocklist_hits == ["cure", "miracle"]
    assert "Remove the blocked term: miracle" in out.required_edits
    # The input audit is not mutated.
    assert APPROVED_AUDIT.approved is True


def test_enforce_blocklist_passthrough_when_clean():
    assert _enforce_blocklist(APPROVED_AUDIT, []) is APPROVED_AUDIT


# ---------------------------------------------------------------------------
# Revise node
# ---------------------------------------------------------------------------

def test_revise_dedupes_notes():
    node = make_revise_node(_deps(FakeLLM()))
    state = PipelineState(
        raw_input="x",
        legal_result=REJECTED_AUDIT,
        brand_result=REJECTED_AUDIT,
        blocklist_hits=["miracle"],
        attempt=1,
    )
    out = node(state)
    assert out["refine_notes"] == [
        "Say less",
        "Remove the blocked term: miracle",
    ]


def test_revise_fallback_note():
    node = make_revise_node(_deps(FakeLLM()))
    state = PipelineState(
        raw_input="x",
        legal_result=NO_EDIT_REJECT,
        brand_result=NO_EDIT_REJECT,
        attempt=1,
    )
    out = node(state)
    assert out["refine_notes"] == [
        "Address the audit findings and produce a safer, on-brand rewrite."
    ]


# ---------------------------------------------------------------------------
# Audit loops and escalation
# ---------------------------------------------------------------------------

def test_legal_rejection_sends_required_edits_to_creative():
    llm = FakeLLM(
        copies=(GOOD, GOOD),
        legal=(REJECTED_AUDIT, APPROVED_AUDIT),
        brand=(APPROVED_AUDIT,),
    )
    result = _start(_app(llm))
    assert "__interrupt__" in result
    assert _get(result, "attempt") == 2
    second_creative = llm.calls_for("creative")[1]
    assert "Say less" in second_creative.user


def test_brand_rejection_sends_required_edits_to_creative():
    llm = FakeLLM(
        copies=(GOOD, GOOD),
        legal=(APPROVED_AUDIT,),
        brand=(REJECTED_AUDIT, APPROVED_AUDIT),
    )
    result = _start(_app(llm))
    assert "__interrupt__" in result
    assert _get(result, "attempt") == 2
    second_creative = llm.calls_for("creative")[1]
    assert "Say less" in second_creative.user


def test_three_rejections_escalate_without_interrupt():
    llm = FakeLLM(copies=(GOOD, GOOD, GOOD), legal=(REJECTED_AUDIT,))
    result = _start(_app(llm))
    assert "__interrupt__" not in result
    assert _get(result, "status") == ESCALATED
    assert _get(result, "attempt") == MAX_ATTEMPTS
    assert _stages_of(result).count("creative") == MAX_ATTEMPTS
    assert _stages_of(result)[-1] == "escalate"


def test_escalate_skips_human_gate():
    llm = FakeLLM(copies=(GOOD, GOOD, GOOD), legal=(REJECTED_AUDIT,))
    result = _start(_app(llm))
    assert "human_gate" not in _stages_of(result)
    assert "request_human" not in _stages_of(result)


def test_escalate_history_detail():
    llm = FakeLLM(copies=(GOOD, GOOD, GOOD), legal=(REJECTED_AUDIT,))
    result = _start(_app(llm))
    assert _get(result["history"][-1], "detail") == "no approval after 3 attempt(s)"


def test_max_attempts_is_three():
    assert MAX_ATTEMPTS == 3


def test_escalate_node_direct():
    node = make_escalate_node(_deps(FakeLLM()))
    out = node(PipelineState(raw_input="x", attempt=3))
    assert out["status"] == ESCALATED
    assert _get(out["history"][-1], "detail") == "no approval after 3 attempt(s)"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "error_stage", [None, "creative", "audit", "brand_audit"], ids=str
)
def test_llm_error_fails_the_run(error_stage):
    llm = FakeLLM(error=LLMError("boom"), error_stage=error_stage)
    result = _start(_app(llm))
    assert "__interrupt__" not in result
    assert _get(result, "status") == FAILED
    assert "LLMError: boom" == _get(result, "error")
    assert _get(result["history"][-1], "status") == "error"


def test_error_stops_downstream_stages():
    llm = FakeLLM(error=LLMError("boom"), error_stage="intake")
    _start(_app(llm))
    assert llm.stages == ["intake"]


def test_quota_exceeded_fails_the_run():
    llm = FakeLLM(error=QuotaExceeded("daily quota reached"))
    result = _start(_app(llm))
    assert _get(result, "status") == FAILED
    assert "QuotaExceeded" in _get(result, "error")
    assert "__interrupt__" not in result


def test_invalid_model_output_fails_the_run():
    llm = FakeLLM(copies=({"headline": "x"},))
    result = _start(_app(llm))
    assert _get(result, "status") == FAILED
    assert "__interrupt__" not in result
    assert _get(result["history"][-1], "status") == "error"


# ---------------------------------------------------------------------------
# Human gate
# ---------------------------------------------------------------------------

def test_unknown_decision_fails():
    app = _app(FakeLLM())
    _start(app)
    final = _resume(app, {"decision": "maybe"})
    assert _get(final, "status") == FAILED
    assert _get(final, "error") == "Unknown human decision 'maybe'"


def test_non_dict_payload_fails():
    app = _app(FakeLLM())
    _start(app)
    final = _resume(app, True)
    assert _get(final, "status") == FAILED
    assert _get(final, "error").startswith(
        "Human decision payload must be a dict, got bool"
    )


def test_invalid_edited_copy_fails():
    app = _app(FakeLLM())
    _start(app)
    final = _resume(app, {"decision": "edit", "edited_copy": {"headline": "x"}})
    assert _get(final, "status") == FAILED
    assert _get(final, "error").startswith("Invalid edited_copy:")


def test_edit_accepts_adcopy_instance_and_resets_attempt():
    app = _app(FakeLLM(copies=(GOOD, GOOD, GOOD)))
    _start(app)
    result = _resume(app, {"decision": "edit", "edited_copy": EDITED})
    assert "__interrupt__" in result
    state = app.get_state(new_thread_config("t")).values
    assert _get(state, "attempt") == 0
    assert _get(state["ad_copy"], "headline") == "Edited headline"
    assert _get(state, "status") == AWAITING_HUMAN


def test_edit_into_blocklisted_copy_loops_through_audit():
    app = _app(FakeLLM(copies=(GOOD, GOOD, GOOD)))
    _start(app)
    _resume(app, {"decision": "edit", "edited_copy": BAD})
    state = app.get_state(new_thread_config("t")).values
    # initial clean pass + the edited BAD copy caught + the post-revise clean pass
    assert _stages_of(state).count("blocklist") == 3
    assert _get(state, "blocklist_hits") == []
    assert _get(state["ad_copy"], "headline") == "Fresh roast"
    assert _get(state, "status") == AWAITING_HUMAN


def test_human_gate_interrupt_payload():
    app = _app(FakeLLM())
    result = _start(app)
    payload = _interrupt_payload(result)
    assert payload["ad_copy"]["headline"] == "Fresh roast"
    assert payload["legal_audit"]["approved"] is True
    assert payload["brand_audit"]["approved"] is True
    assert payload["blocklist_hits"] == []
    assert payload["attempt"] == 1
    assert "'approve'" in payload["instruction"]


# ---------------------------------------------------------------------------
# Graph plumbing / routers / short-circuits
# ---------------------------------------------------------------------------

def test_build_graph_uses_given_checkpointer():
    saver = InMemorySaver()
    app = build_graph(deps=_deps(FakeLLM()), checkpointer=saver)
    assert app.checkpointer is saver


def test_run_pipeline_helper_interrupts():
    result = run_pipeline("A coffee brand brief", deps=_deps(FakeLLM()))
    assert "__interrupt__" in result
    assert _get(result, "status") == AWAITING_HUMAN


def test_new_thread_config_uniqueness_and_override():
    first = new_thread_config()
    second = new_thread_config()
    assert first["configurable"]["thread_id"] != second["configurable"]["thread_id"]
    assert new_thread_config("abc")["configurable"]["thread_id"] == "abc"


def test_threads_are_isolated():
    app = _app(FakeLLM())
    first = _start(app, raw="brief one", thread_id="t1")
    second = _start(app, raw="brief two", thread_id="t2")
    assert _get(first, "status") == AWAITING_HUMAN
    assert _get(second, "status") == AWAITING_HUMAN

    _resume(app, {"decision": "approve"}, "t1")
    first_state = app.get_state(new_thread_config("t1")).values
    second_state = app.get_state(new_thread_config("t2")).values
    assert _get(first_state, "status") == APPROVED
    assert _get(second_state, "status") == AWAITING_HUMAN


def test_pipeline_state_requires_raw_input():
    with pytest.raises(ValidationError):
        PipelineState()


@pytest.mark.parametrize(
    "factory",
    [
        make_intake_node,
        make_creative_node,
        make_blocklist_node,
        make_legal_audit_node,
        make_brand_audit_node,
        make_revise_node,
        make_request_human_node,
    ],
)
def test_failed_state_short_circuits_nodes(factory):
    node = factory(_deps(FakeLLM()))
    assert node(PipelineState(raw_input="x", status=FAILED)) == {}


def test_creative_clears_stale_results():
    node = make_creative_node(_deps(FakeLLM()))
    state = PipelineState(
        raw_input="x",
        brief=BRIEF,
        ad_copy=BAD,
        legal_result=REJECTED_AUDIT,
        brand_result=REJECTED_AUDIT,
        blocklist_hits=["miracle"],
        human_decision="approve",
        attempt=1,
    )
    out = node(state)
    assert out["legal_result"] is None
    assert out["brand_result"] is None
    assert out["blocklist_hits"] == []
    assert out["human_decision"] is None
    assert out["attempt"] == 2
    assert out["status"] == RUNNING


def test_route_after_audits_failed_ends():
    assert route_after_audits(PipelineState(raw_input="x", status=FAILED)) == END


def test_route_after_audits_missing_results_escalate():
    state = PipelineState(raw_input="x", legal_result=APPROVED_AUDIT)
    assert route_after_audits(state) == "escalate"


def test_route_after_audits_approved_requests_human():
    state = PipelineState(
        raw_input="x",
        legal_result=APPROVED_AUDIT,
        brand_result=APPROVED_AUDIT,
    )
    assert route_after_audits(state) == "request_human"


def test_route_after_audits_rejected_below_cap_revises():
    state = PipelineState(
        raw_input="x",
        legal_result=REJECTED_AUDIT,
        brand_result=APPROVED_AUDIT,
        attempt=1,
    )
    assert route_after_audits(state) == "revise"


def test_route_after_audits_rejected_at_cap_escalates():
    state = PipelineState(
        raw_input="x",
        legal_result=REJECTED_AUDIT,
        brand_result=APPROVED_AUDIT,
        attempt=MAX_ATTEMPTS,
    )
    assert route_after_audits(state) == "escalate"


def test_route_after_human_edit_reenters_blocklist():
    state = PipelineState(raw_input="x", human_decision="edit", status=RUNNING)
    assert route_after_human(state) == "blocklist"


def test_route_after_human_terminal_ends():
    approved = PipelineState(raw_input="x", human_decision="approve", status=APPROVED)
    assert route_after_human(approved) == END


def test_route_after_human_failed_ends():
    state = PipelineState(raw_input="x", status=FAILED)
    assert route_after_human(state) == END
