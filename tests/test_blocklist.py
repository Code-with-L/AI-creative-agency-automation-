"""Hard-blocklist tests. No network, no cost."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.blocklist import BLOCKLIST_TERMS, check_blocklist, is_blocked

FIXTURES = Path(__file__).parent / "fixtures"


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _ad_text(ad: dict) -> str:
    parts = [ad.get("headline", ""), ad.get("body", ""), ad.get("cta", "")]
    parts.extend(ad.get("disclosures", []))
    return "\n".join(parts)


def test_blocklist_not_empty():
    assert BLOCKLIST_TERMS
    assert all(t.strip() for t in BLOCKLIST_TERMS)


@pytest.mark.parametrize("term", ["cure", "miracle", "guaranteed results"])
def test_case_insensitive(term):
    assert term in check_blocklist(f"Mention {term.upper()} here.")


@pytest.mark.parametrize("text", ["secure checkout", "manicure tips", "obscure fact"])
def test_whole_word_avoids_substring_false_positives(text):
    assert check_blocklist(text) == []
    assert not is_blocked(text)


def test_clean_copy_has_no_hits():
    assert check_blocklist("A friendly, honest ad about coffee beans.") == []


def test_all_pass_ads_are_clean():
    for raw in _load(FIXTURES / "pass_ads.json"):
        assert check_blocklist(_ad_text(raw)) == [], raw["headline"]


def test_all_heldout_pass_ads_are_clean():
    for raw in _load(FIXTURES / "heldout" / "pass_ads.json"):
        assert check_blocklist(_ad_text(raw)) == [], raw["headline"]


def test_all_fail_ads_trigger_and_match_expected():
    for raw in _load(FIXTURES / "fail_ads.json"):
        hits = check_blocklist(_ad_text(raw["ad"]))
        assert hits, raw["ad"]["headline"]
        assert set(raw["expected_blocklist_hits"]) <= set(hits), raw["ad"]["headline"]


def test_all_heldout_fail_ads_trigger_and_match_expected():
    for raw in _load(FIXTURES / "heldout" / "fail_ads.json"):
        hits = check_blocklist(_ad_text(raw["ad"]))
        assert hits, raw["ad"]["headline"]
        assert set(raw["expected_blocklist_hits"]) <= set(hits), raw["ad"]["headline"]
