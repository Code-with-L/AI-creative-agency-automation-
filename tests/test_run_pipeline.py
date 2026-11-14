"""Tests for the creative pipeline CLI (no network, no cost)."""

import importlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_script():
    return importlib.import_module("src.cli")


@pytest.fixture(scope="module")
def rp():
    return _load_script()


def test_approve_returns_zero(rp, capsys):
    code = rp.main(["--auto-decision", "approve"])
    out = capsys.readouterr().out

    assert code == 0
    assert "Final: APPROVED by a human." not in out
    assert rp.AUTO_APPROVED_LABEL in out


def test_reject_returns_three(rp, capsys):
    code = rp.main(["--auto-decision", "reject"])
    out = capsys.readouterr().out

    assert code == 3
    assert "REJECTED" in out


def test_edit_terminates_and_labels_auto_approval(rp, capsys):
    code = rp.main(["--auto-decision", "edit"])
    out = capsys.readouterr().out

    assert code == 0
    assert rp.AUTO_APPROVED_LABEL in out
    assert "Final: APPROVED by a human." not in out
    assert "auto_decision" in out
    assert "test mode: edit" in out


def test_gate_cap_returns_two(rp, capsys, monkeypatch):
    monkeypatch.setattr(rp, "MAX_GATES", 0)
    code = rp.main(["--auto-decision", "approve"])
    out = capsys.readouterr().out

    assert code == 2
    assert "exceeded 0 human gates" in out


def test_live_gate_cap_is_lower(rp):
    assert rp.MAX_GATES_LIVE == 5
    assert rp.MAX_GATES == 20
    assert rp.max_gates(live=True) == 5
    assert rp.max_gates(live=False) == 20


def test_auto_decision_refused_with_live(rp, capsys):
    code = rp.main(["--live", "--auto-decision", "approve"])
    err = capsys.readouterr().err

    assert code == 6
    assert "cannot be combined with --live" in err


CUSTOM_COPY = {
    "headline": "Better espresso at home, made simple.",
    "body": (
        "Dialing in a great shot starts with good coffee. Every month, we deliver "
        "a distinct single-origin bag to your door. It's an easy way to explore new "
        "flavor notes and elevate your morning cup. Cancel anytime."
    ),
    "cta": "Start Your Subscription",
    "disclosures": [],
}


def test_edited_copy_file_is_submitted_on_edit(rp, tmp_path, capsys):
    path = tmp_path / "edited_copy.json"
    path.write_text(json.dumps(CUSTOM_COPY), encoding="utf-8")

    code = rp.main(["--auto-decision", "edit", "--edited-copy", str(path)])
    out = capsys.readouterr().out
    result = out.split("--- Result ---", 1)[-1]

    assert code == 0
    assert CUSTOM_COPY["headline"] in result
    assert CUSTOM_COPY["body"] in result
    assert CUSTOM_COPY["cta"] in result
    assert rp.DEMO_CLEAN_COPY.body not in result


def test_prompt_decision_edit_falls_back_to_demo_copy(rp):
    payload = rp.prompt_decision("edit")

    assert payload == {
        "decision": "edit",
        "edited_copy": rp.DEMO_CLEAN_COPY.model_dump(),
    }


def test_edited_copy_missing_file_exits(rp, tmp_path):
    missing = tmp_path / "nope.json"

    with pytest.raises(SystemExit) as excinfo:
        rp.main(["--auto-decision", "edit", "--edited-copy", str(missing)])

    assert "--edited-copy file not found" in str(excinfo.value)


def test_edited_copy_invalid_json_exits(rp, tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(SystemExit) as excinfo:
        rp.main(["--auto-decision", "edit", "--edited-copy", str(path)])

    assert "not valid JSON" in str(excinfo.value)


def test_edited_copy_wrong_shape_exits(rp, tmp_path):
    path = tmp_path / "wrong.json"
    path.write_text(json.dumps({"headline": "only a headline"}), encoding="utf-8")

    with pytest.raises(SystemExit) as excinfo:
        rp.main(["--auto-decision", "edit", "--edited-copy", str(path)])

    assert "does not match AdCopy" in str(excinfo.value)

