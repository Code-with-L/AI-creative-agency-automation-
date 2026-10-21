"""Tests for the scripts/run_pipeline.py CLI (no network, no cost)."""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "run_pipeline.py"


def _load_script():
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    spec = importlib.util.spec_from_file_location("run_pipeline_script", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


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
