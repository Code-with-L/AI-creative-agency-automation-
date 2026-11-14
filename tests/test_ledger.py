"""Tests for the Phase 3 audit ledger."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.ledger import (
    DEFAULT_LEDGER_PATH,
    append_entry,
    build_entry,
    read_entries,
    summarise,
    unsafe_approvals,
)
from src.pipeline.state import HistoryEntry
from src.schemas import AdCopy, AuditResult


def _ad():
    return AdCopy(headline="H", body="B", cta="C", disclosures=[])


def _audit(approved=True, hits=None):
    return AuditResult(
        approved=approved,
        risk_score=2 if approved else 8,
        required_edits=[],
        reasons=["ok"],
        blocklist_hits=list(hits or []),
    )


def _state():
    return {
        "raw_input": "Client: Acme.",
        "status": "approved",
        "attempt": 2,
        "ad_copy": _ad(),
        "blocklist_hits": [],
        "legal_result": _audit(),
        "brand_result": _audit(),
        "human_decision": "approve",
        "error": None,
        "history": [
            HistoryEntry(stage="intake", model="demo-model", latency_ms=12.5, attempt=1),
            HistoryEntry(stage="creative", model="demo-model", latency_ms=40.0, attempt=1),
            {"stage": "blocklist", "model": None, "latency_ms": 0.25, "attempt": 1,
             "status": "ok", "detail": "clean"},
            {"stage": "bogus", "latency_ms": "not-a-number"},
        ],
    }


def test_build_entry_from_mixed_state_and_dicts(tmp_path):
    entry = build_entry(_state(), mode="demo")
    assert entry.schema_version == 1
    assert entry.mode == "demo"
    assert entry.status == "approved"
    assert entry.attempts == 2
    assert entry.ad_copy is not None and entry.ad_copy.headline == "H"
    assert entry.legal_result is not None and entry.legal_result.approved
    assert entry.human_decision == "approve"
    assert len(entry.history) == 3  # the malformed "bogus" entry is dropped, not fatal
    assert entry.latency_ms_total == pytest.approx(52.75)


def test_append_and_read_roundtrip(tmp_path):
    ledger = tmp_path / "sub" / "ledger.jsonl"
    first = build_entry(_state(), mode="demo")
    second = build_entry(_state(), mode="live")
    append_entry(ledger, first)
    append_entry(ledger, second)

    entries = read_entries(ledger)
    assert [e.mode for e in entries] == ["demo", "live"]
    assert entries[0].run_id == first.run_id
    assert entries[0].ad_copy == first.ad_copy

    lines = Path(ledger).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    json.loads(lines[0])  # valid JSON, parseable independently


def test_read_missing_ledger_returns_empty(tmp_path):
    assert read_entries(tmp_path / "missing.jsonl") == []


def test_corrupt_line_fails_loudly_with_path_and_line(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(
        json.dumps(build_entry(_state(), mode="demo").model_dump()) + "\nnot json\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match=r"ledger\.jsonl:2"):
        read_entries(ledger)


def test_state_must_be_a_mapping(tmp_path):
    with pytest.raises(TypeError):
        build_entry("brief", mode="demo")


def test_summarise_counts_by_status():
    state = _state()
    entries = [
        build_entry({**state, "status": "approved"}, mode="demo"),
        build_entry({**state, "status": "rejected"}, mode="demo"),
        build_entry({**state, "status": "rejected"}, mode="demo"),
    ]
    counts = summarise(entries)
    assert counts["approved"] == 1
    assert counts["rejected"] == 2


def test_unsafe_approvals_flags_approved_with_blocklist_hits():
    state = _state()
    hits = ["guarantee"]
    bad = build_entry(
        {**state, "status": "approved", "blocklist_hits": hits}, mode="demo"
    )
    good = build_entry(
        {**state, "status": "approved", "blocklist_hits": []}, mode="demo"
    )
    assert unsafe_approvals([bad, good]) == [bad]


def test_default_path_is_gitignored():
    assert DEFAULT_LEDGER_PATH.name == "ledger.jsonl"
    gitignore = Path(__file__).resolve().parents[1] / ".gitignore"
    content = gitignore.read_text(encoding="utf-8")
    assert ".data/" in content
    assert "*.jsonl" in content
