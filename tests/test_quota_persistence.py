"""Persistence tests for the cross-process daily quota guard."""
from __future__ import annotations

import json
import warnings
from datetime import date

import pytest

from src.config import QuotaState, Settings


def test_consume_persists_and_reloads(tmp_path):
    state_file = tmp_path / "quota.json"
    first = QuotaState(daily_quota=5, path=str(state_file))
    first.consume(2)
    assert json.loads(state_file.read_text(encoding="utf-8")) == {
        "day": date.today().isoformat(),
        "used": 2,
    }

    reloaded = QuotaState(daily_quota=5, path=str(state_file))
    assert reloaded.used == 2
    assert reloaded.remaining() == 3


def test_stale_day_resets_counter(tmp_path):
    state_file = tmp_path / "quota.json"
    state_file.write_text(
        json.dumps({"day": "1999-01-01", "used": 49}), encoding="utf-8"
    )
    state = QuotaState(daily_quota=50, path=str(state_file))
    assert state.used == 0
    assert state.remaining() == 50


def test_corrupt_file_warns_and_starts_fresh(tmp_path):
    state_file = tmp_path / "quota.json"
    state_file.write_text("not json at all", encoding="utf-8")
    with pytest.warns(UserWarning, match="unreadable quota state"):
        state = QuotaState(daily_quota=50, path=str(state_file))
    assert state.used == 0


def test_save_failure_warns_but_keeps_counting(tmp_path):
    # A directory in the place of the file makes both load and write fail.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        state = QuotaState(daily_quota=50, path=str(tmp_path))
    with pytest.warns(UserWarning, match="Could not persist quota state"):
        state.consume(3)
    assert state.used == 3


def test_exhausted_budget_blocks_calls(tmp_path):
    state_file = tmp_path / "quota.json"
    state = QuotaState(daily_quota=2, path=str(state_file))
    state.consume(2)
    assert not state.has_capacity()
    reloaded = QuotaState(daily_quota=2, path=str(state_file))
    assert not reloaded.has_capacity()


def test_env_wires_quota_state_path(monkeypatch, tmp_path):
    target = tmp_path / "custom.json"
    monkeypatch.setenv("LLM_QUOTA_STATE_PATH", str(target))
    settings = Settings.from_env()
    assert settings.quota_state_path == str(target)
