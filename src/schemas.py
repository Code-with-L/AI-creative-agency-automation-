"""Pydantic models for Phase 1."""
from __future__ import annotations

from typing import Any, List, Optional

from pydantic import BaseModel, Field, field_validator

# Values models commonly emit instead of "not stated". They are normalised to
# None so a missing fact never masquerades as a real answer.
PLACEHOLDER_VALUES = {"", "unspecified", "n/a", "unknown", "tbd", "none", "null"}


def _clean_text(value: Any) -> Optional[str]:
    """Coerce one value to a stripped string, or None when it is a placeholder.

    Lists/tuples are joined into one comma-separated phrase (models sometimes
    return several tone descriptors as an array).
    """
    if isinstance(value, (list, tuple, set)):
        parts = [text for text in (_clean_text(item) for item in value) if text]
        return ", ".join(parts) if parts else None
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in PLACEHOLDER_VALUES:
        return None
    return text


def _clean_list(value: Any) -> List[str]:
    """Coerce a scalar, list or None into a list of non-placeholder strings."""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        items = [value]
    cleaned: List[str] = []
    for item in items:
        text = _clean_text(item)
        if text:
            cleaned.append(text)
    return cleaned


class CreativeBrief(BaseModel):
    """A raw, messy client brief normalised into structured fields.

    Fields the brief does not state stay None (never a placeholder such as
    "Unspecified"), so downstream stages can tell "unknown" from "answered".
    """

    client: Optional[str] = None
    product: Optional[str] = None
    audience: Optional[str] = None
    goal: Optional[str] = None
    tone: Optional[str] = None
    channels: List[str] = Field(default_factory=list)
    must_include: List[str] = Field(default_factory=list)
    avoid: List[str] = Field(default_factory=list)
    raw_text: Optional[str] = None

    @field_validator("client", "product", "audience", "goal", "tone", mode="before")
    @classmethod
    def _normalise_text(cls, value: Any) -> Optional[str]:
        return _clean_text(value)

    @field_validator("channels", "must_include", "avoid", mode="before")
    @classmethod
    def _normalise_list(cls, value: Any) -> List[str]:
        return _clean_list(value)


class AdCopy(BaseModel):
    """A single ad creative."""

    headline: str = Field(..., min_length=1)
    body: str = Field(..., min_length=1)
    cta: str = Field(..., min_length=1)
    disclosures: List[str] = Field(default_factory=list)


class AuditResult(BaseModel):
    """Output of a legal or brand audit.

    This is a first-pass filter only. `reviewed_by_human` must remain False
    until an actual person signs off (see project rule 8).
    """

    approved: bool
    risk_score: int = Field(..., ge=1, le=10)
    required_edits: List[str] = Field(default_factory=list)
    reasons: List[str] = Field(default_factory=list)
    blocklist_hits: List[str] = Field(default_factory=list)
    reviewed_by_human: bool = False
