"""Schema and fixture-integrity tests. No network, no cost."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.config import QuotaState, Settings
from src.llm import call_llm
from src.schemas import AdCopy, AuditResult, CreativeBrief

FIXTURES = Path(__file__).parent / "fixtures"


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


class _FakeResponse:
    def __init__(self, text: str):
        self.status_code = 200
        self.text = text
        self._payload = {"candidates": [{"content": {"parts": [{"text": text}]}}]}

    def json(self):
        return self._payload


def test_fifteen_briefs_validate():
    briefs = _load(FIXTURES / "briefs.json")
    assert len(briefs) == 15
    for raw in briefs:
        brief = CreativeBrief.model_validate(raw)
        for name in ("client", "product", "audience", "goal", "tone"):
            expected = raw.get(name)
            if isinstance(expected, str):
                expected = expected.strip()
            assert getattr(brief, name) == expected
        assert brief.channels == raw.get("channels", [])
        assert brief.must_include == raw.get("must_include", [])
        assert brief.avoid == raw.get("avoid", [])


@pytest.mark.parametrize(
    "value", ["Unspecified", " unspecified ", "N/A", "unknown", "TBD", "none", "null", ""]
)
def test_placeholder_client_becomes_none(value):
    brief = CreativeBrief.model_validate({"client": value, "product": "Beans"})
    assert brief.client is None
    assert brief.product == "Beans"


def test_missing_fields_stay_none_and_lists_stay_empty():
    brief = CreativeBrief.model_validate({})
    assert brief.client is None
    assert brief.product is None
    assert brief.audience is None
    assert brief.goal is None
    assert brief.tone is None
    assert brief.channels == []
    assert brief.must_include == []
    assert brief.avoid == []


def test_tone_list_is_joined_into_one_string():
    brief = CreativeBrief.model_validate({"tone": ["warm", "crafty"]})
    assert brief.tone == "warm, crafty"


def test_placeholder_list_items_are_dropped_before_joining():
    brief = CreativeBrief.model_validate({"tone": ["warm", "N/A", " None "]})
    assert brief.tone == "warm"


def test_scalar_channel_becomes_single_item_list():
    brief = CreativeBrief.model_validate({"channels": "instagram"})
    assert brief.channels == ["instagram"]


def test_placeholder_list_items_are_removed():
    brief = CreativeBrief.model_validate(
        {"channels": ["instagram", "TBD", "", "email"], "avoid": "N/A"}
    )
    assert brief.channels == ["instagram", "email"]
    assert brief.avoid == []


def test_whitespace_is_stripped_from_scalar_fields():
    brief = CreativeBrief.model_validate(
        {"client": "  Northwind Coffee  ", "goal": "\tDrive orders\n"}
    )
    assert brief.client == "Northwind Coffee"
    assert brief.goal == "Drive orders"


@pytest.mark.parametrize("index", range(5))
def test_intake_extracts_client_for_named_briefs(index):
    raw = _load(FIXTURES / "briefs.json")[index]
    text = json.dumps(raw)
    result = call_llm(
        "intake",
        "system prompt",
        raw.get("raw_text", text),
        json_mode=True,
        response_model=CreativeBrief,
        settings=Settings(api_key="test-key", model_intake="test-model"),
        quota=QuotaState(100),
        http_post=lambda *args, **kwargs: _FakeResponse(text),
        trace_hook=lambda trace: None,
    )
    assert isinstance(result, CreativeBrief)
    assert result.client == raw["client"]
    assert "unspecified" not in (result.client or "").lower()


def test_ten_pass_ads_validate():
    ads = _load(FIXTURES / "pass_ads.json")
    assert len(ads) == 10
    for raw in ads:
        AdCopy.model_validate(raw)


def test_heldout_fixtures_present():
    assert len(_load(FIXTURES / "heldout" / "pass_ads.json")) == 5
    assert len(_load(FIXTURES / "heldout" / "fail_ads.json")) == 5


@pytest.mark.parametrize("score", [1, 10])
def test_risk_score_bounds_ok(score):
    result = AuditResult(approved=True, risk_score=score)
    assert result.risk_score == score
    assert result.reviewed_by_human is False


@pytest.mark.parametrize("score", [0, 11, -3])
def test_risk_score_out_of_bounds_raises(score):
    with pytest.raises(ValidationError):
        AuditResult(approved=True, risk_score=score)
