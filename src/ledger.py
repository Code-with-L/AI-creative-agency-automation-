"""Phase 3: an append-only audit ledger for pipeline runs.

The ledger is the operational side of the safety model: after any run it
records what was generated, what the audits concluded, what the human did,
and how long each stage took. Entries are one JSON object per line so the
file can be appended atomically-ish, rotated, or grep'd without a database.

The ledger contains client briefs and generated copy, so it is gitignored
(see .gitignore -> .data/) and must never be committed. A corrupt line makes
``read_entries`` fail loudly with the offending path:line instead of
silently dropping runs from the audit trail.
"""
from __future__ import annotations

import json
import os
from collections import Counter
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .pipeline.state import HistoryEntry
from .schemas import AdCopy, AuditResult

LEDGER_SCHEMA_VERSION = 1
DEFAULT_LEDGER_PATH = Path(".data") / "ledger.jsonl"


class LedgerEntry(BaseModel):
    """One auditable record for one pipeline run."""

    schema_version: int = LEDGER_SCHEMA_VERSION
    run_id: str = Field(default_factory=lambda: os.urandom(16).hex())
    created_at: str = Field(
        default_factory=lambda:
        datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    )
    mode: str
    raw_input: str
    status: str
    attempts: int = 0
    ad_copy: AdCopy | None = None
    blocklist_hits: list[str] = Field(default_factory=list)
    legal_result: AuditResult | None = None
    brand_result: AuditResult | None = None
    human_decision: str | None = None
    error: str | None = None
    latency_ms_total: float = 0.0
    history: list[HistoryEntry] = Field(default_factory=list)


def _coerce_model(model_cls: type, value: Any):
    """Accept a model instance, a dict, or a ``model_dump()``-like mapping."""
    if value is None:
        return None
    if isinstance(value, model_cls):
        return value
    if isinstance(value, Mapping):
        return model_cls.model_validate(dict(value))
    return model_cls.model_validate(value)


def _coerce_history(items: Any) -> list[HistoryEntry]:
    out: list[HistoryEntry] = []
    for item in items or []:
        if isinstance(item, HistoryEntry):
            out.append(item)
            continue
        if isinstance(item, Mapping):
            try:
                out.append(HistoryEntry.model_validate(dict(item)))
            except Exception:  # one bad stage must not sink the audit trail
                continue
    return out


def build_entry(
    state: Mapping[str, Any],
    *,
    mode: str,
    raw_input: str | None = None,
) -> LedgerEntry:
    """Build a ``LedgerEntry`` from a (possibly partial) graph state dict."""
    if not isinstance(state, Mapping):
        raise TypeError(f"state must be a mapping, got {type(state).__name__}")

    history = _coerce_history(state.get("history"))
    raw = raw_input if raw_input is not None else state.get("raw_input") or ""
    return LedgerEntry(
        mode=mode,
        raw_input=str(raw),
        status=str(state.get("status") or "unknown"),
        attempts=int(state.get("attempt") or 0),
        ad_copy=_coerce_model(AdCopy, state.get("ad_copy")),
        blocklist_hits=list(state.get("blocklist_hits") or []),
        legal_result=_coerce_model(AuditResult, state.get("legal_result")),
        brand_result=_coerce_model(AuditResult, state.get("brand_result")),
        human_decision=state.get("human_decision"),
        error=state.get("error"),
        latency_ms_total=sum(float(e.latency_ms or 0.0) for e in history),
        history=history,
    )


def append_entry(path: Any, entry: LedgerEntry) -> Path:
    """Append ``entry`` as one JSON line, creating parent dirs as needed."""
    ledger_path = Path(path)
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(entry.model_dump(), ensure_ascii=False, sort_keys=True)
    with ledger_path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(line)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    return ledger_path


def read_entries(path: Any) -> list[LedgerEntry]:
    """Read every entry; a corrupt line fails loudly with path:line."""
    ledger_path = Path(path)
    if not ledger_path.exists():
        return []
    entries: list[LedgerEntry] = []
    for lineno, line in enumerate(
        ledger_path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(LedgerEntry.model_validate(json.loads(line)))
        except Exception as exc:
            raise ValueError(
                f"{ledger_path}:{lineno}: invalid ledger entry ({exc})"
            ) from exc
    return entries


def summarise(entries: list[LedgerEntry]) -> Counter:
    """Count runs by terminal status."""
    return Counter(entry.status for entry in entries)


def unsafe_approvals(entries: list[LedgerEntry]) -> list[LedgerEntry]:
    """Approved runs that still carry blocklist hits.

    This is the one combination that must never ship; the deterministic
    blocklist is the floor, so approving past a hit is an anomaly.
    """
    return [
        entry
        for entry in entries
        if entry.status == "approved" and entry.blocklist_hits
    ]
