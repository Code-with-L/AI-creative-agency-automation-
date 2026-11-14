"""Tests for the pydantic -> Gemini responseSchema converter (opt-in)."""
from __future__ import annotations

import pytest
from pydantic import BaseModel

from src.response_schema import to_gemini_response_schema
from src.schemas import AdCopy, AuditResult, CreativeBrief


def test_ad_copy_schema_converts():
    schema = to_gemini_response_schema(AdCopy)
    assert schema == {
        "type": "object",
        "properties": {
            "headline": {"type": "string"},
            "body": {"type": "string"},
            "cta": {"type": "string"},
            "disclosures": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["headline", "body", "cta"],
    }


def test_creative_brief_optional_strings_are_nullable():
    schema = to_gemini_response_schema(CreativeBrief)
    assert schema["type"] == "object"
    for field in ("client", "product", "audience", "goal", "tone"):
        assert schema["properties"][field]["nullable"] is True
    assert schema["properties"]["channels"] == {"type": "array", "items": {"type": "string"}}
    assert schema["properties"]["must_include"]["type"] == "array"
    assert schema["properties"]["avoid"]["type"] == "array"


def test_audit_result_required_and_types():
    schema = to_gemini_response_schema(AuditResult)
    assert schema["properties"]["approved"] == {"type": "boolean"}
    assert schema["properties"]["risk_score"] == {"type": "integer"}
    assert schema["properties"]["required_edits"]["type"] == "array"
    assert schema["properties"]["reasons"]["items"] == {"type": "string"}
    assert "approved" in schema["required"]
    assert "risk_score" in schema["required"]
    # Fields with defaults are not required.
    assert "required_edits" not in schema["required"]


def test_unconvertible_model_refuses_loudly():
    class DictHolder(BaseModel):
        payload: dict[str, str]

    with pytest.raises(ValueError, match="DictHolder"):
        to_gemini_response_schema(DictHolder)


def test_nested_model_converts_inline():
    class Inner(BaseModel):
        value: str

    class Outer(BaseModel):
        inner: Inner
        note: str | None = None

    schema = to_gemini_response_schema(Outer)
    assert schema["properties"]["inner"]["type"] == "object"
    assert schema["properties"]["inner"]["properties"]["value"] == {"type": "string"}
    assert schema["properties"]["note"]["nullable"] is True
