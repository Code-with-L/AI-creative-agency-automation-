"""Tests for the Phase 4 deliverable export."""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

from src.ledger import append_entry, build_entry
from src.schemas import AdCopy, AuditResult

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

cli = importlib.import_module("src.export_deliverable")


def _entry(status="approved", hits=None, run_id=None, stamp="2026-01-01T00:00:00Z", cta="Buy"):
    ad = AdCopy(headline="H", body="B", cta=cta, disclosures=["T&Cs apply"])
    state = {
        "raw_input": "Client: Acme.",
        "status": status,
        "attempt": 1,
        "ad_copy": ad,
        "blocklist_hits": list(hits or []),
        "legal_result": AuditResult(approved=True, risk_score=1, required_edits=[], reasons=["ok"], blocklist_hits=[]),
        "brand_result": AuditResult(approved=True, risk_score=2, required_edits=[], reasons=["ok"], blocklist_hits=[]),
        "human_decision": "approve" if status == "approved" else "reject",
        "history": [],
    }
    entry = build_entry(state, mode="demo")
    if run_id:
        entry.run_id = run_id
    entry.created_at = stamp
    return entry


def _ledger(tmp_path, entries):
    path = tmp_path / "ledger.jsonl"
    for e in entries:
        append_entry(path, e)
    return path


def test_exports_latest_clean_approved(tmp_path):
    ledger = _ledger(tmp_path, [
        _entry(stamp="2026-01-01T00:00:00Z", cta="First"),
        _entry(status="rejected", stamp="2026-01-03T00:00:00Z", cta="Rejected"),
        _entry(stamp="2026-01-02T00:00:00Z", cta="Latest ok"),
    ])
    out = tmp_path / "out"
    rc = cli.main(["--ledger", str(ledger), "--out", str(out)])
    assert rc == 0
    exported = list(out.iterdir())
    assert len(exported) == 1
    payload = json.loads((exported[0] / "ad_copy.json").read_text(encoding="utf-8"))
    assert payload["cta"] == "Latest ok"
    assert "T&Cs apply" in (exported[0] / "ad_copy.md").read_text(encoding="utf-8")
    assert "Legal audit" in (exported[0] / "audit_report.md").read_text(encoding="utf-8")
    assert "reviewed by human" in (exported[0] / "audit_report.md").read_text(encoding="utf-8")


def test_no_approved_run_errors(tmp_path):
    ledger = _ledger(tmp_path, [_entry(status="failed")])
    rc = cli.main(["--ledger", str(ledger), "--out", str(tmp_path / "out")])
    assert rc == 1


def test_unapproved_run_id_refused(tmp_path, capsys):
    ledger = _ledger(tmp_path, [_entry(status="rejected", run_id="deadbeef" * 4)])
    rc = cli.main(["--ledger", str(ledger), "--run-id", "deadbeef", "--out", str(tmp_path / "out")])
    assert rc == 2
    assert "not approved" in capsys.readouterr().err


def test_unsafe_approval_refused_by_default(tmp_path):
    ledger = _ledger(tmp_path, [_entry(status="approved", hits=["guarantee"])])
    rc = cli.main(["--ledger", str(ledger), "--out", str(tmp_path / "out")])
    assert rc == 2
    assert not (tmp_path / "out").exists()


def test_unsafe_approval_exported_with_flag(tmp_path):
    ledger = _ledger(tmp_path, [_entry(status="approved", hits=["guarantee"])])
    rc = cli.main(["--ledger", str(ledger), "--out", str(tmp_path / "out"), "--include-unsafe"])
    assert rc == 0


def test_existing_output_never_overwritten(tmp_path):
    entry = _entry(run_id="abc123")
    ledger = _ledger(tmp_path, [entry])
    target = tmp_path / "out" / "abc123"
    target.mkdir(parents=True)
    (target / "marker.txt").write_text("keep me", encoding="utf-8")
    rc = cli.main(["--ledger", str(ledger), "--out", str(tmp_path / "out"), "--run-id", "abc123"])
    assert rc == 3
    assert (target / "marker.txt").read_text(encoding="utf-8") == "keep me"


def test_corrupt_ledger_errors(tmp_path, capsys):
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text("this is not json\n", encoding="utf-8")
    rc = cli.main(["--ledger", str(ledger), "--out", str(tmp_path / "out")])
    assert rc == 1

