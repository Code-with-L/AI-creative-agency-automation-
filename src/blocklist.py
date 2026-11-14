"""Hard blocklist - the last line of defence before human review.

Matching is whole-word and case-insensitive so that legitimate copy is not
flagged by accident (e.g. "cure" must not match inside "secure" or "manicure").
"""
from __future__ import annotations

import re
from re import Pattern

# Words/phrases that must never appear in generated ad copy. This list is the
# hard floor; a legal or brand audit may reject more, never less.
BLOCKLIST_TERMS: list[str] = [
    "cure",
    "cures",
    "guarantee",
    "guaranteed",
    "guaranteed results",
    "miracle",
    "100% effective",
    "100% safe",
    "risk-free",
    "no side effects",
    "instant results",
    "clinically proven",
    "get rich quick",
    "earn thousands",
    "no work required",
    "fda approved",
]


def _compile(term: str) -> Pattern[str]:
    # \b does not behave with % or spaces, so use explicit non-word lookarounds.
    return re.compile(r"(?<!\w)" + re.escape(term) + r"(?!\w)", re.IGNORECASE)


_MATCHERS: list[tuple[str, Pattern[str]]] = [(t, _compile(t)) for t in BLOCKLIST_TERMS]


def check_blocklist(text: str) -> list[str]:
    """Return the blocklist terms found in `text` (empty list when clean)."""
    if not text:
        return []
    return [term for term, rx in _MATCHERS if rx.search(text)]


def is_blocked(text: str) -> bool:
    return bool(check_blocklist(text))
