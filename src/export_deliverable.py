"""Export an approved run from the audit ledger as a client deliverable.

Closes the product loop: the pipeline ends at a human decision, and this
script packages that decision into deterministic artifacts a client (or the
next stage) can consume:

    <out>/<run-id-prefix>/ad_copy.md       headline / body / CTA / disclosures
    <out>/<run-id-prefix>/ad_copy.json     the same copy, machine-readable
    <out>/<run-id-prefix>/audit_report.md  who approved, what the audits said

Safety rules (non-negotiable):

* Only ``approved`` runs are exported.
* Runs that carry blocklist hits are refused unless ``--include-unsafe``.
* An existing output directory is never overwritten.

Defaults to the latest clean approved run in ``.data/ledger.jsonl``:

    python scripts/export_deliverable.py
    python scripts/export_deliverable.py --run-id 5a4f431e --out .data/deliverables
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.ledger import DEFAULT_LEDGER_PATH, LedgerEntry, read_entries  # noqa: E402
from src.pipeline.state import APPROVED  # noqa: E402


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Export an approved run from the ledger.")
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER_PATH,
                        help="path to the ledger JSONL (default: .data/ledger.jsonl)")
    parser.add_argument("--run-id", default=None,
                        help="export this run id prefix instead of the latest approved run")
    parser.add_argument("--out", type=Path, default=Path(".data") / "deliverables",
                        help="output root (default: .data/deliverables)")
    parser.add_argument("--include-unsafe", action="store_true",
                        help="allow exporting approved runs that still carry blocklist hits")
    return parser.parse_args(argv)


def _sanitized_prefix(run_id: str) -> str:
    cleaned = "".join(c for c in run_id[:12] if c.isalnum())
    if not cleaned:
        raise ValueError(f"run id {run_id!r} has no usable characters for a directory")
    return cleaned


def pick_run(entries, run_id, include_unsafe):
    """Return (entry, error_message, refused)."""
    if run_id:
        matches = [e for e in entries if e.run_id.startswith(run_id)]
        if not matches:
            return None, f"no ledger entry matches run id {run_id!r}", True
        entry = sorted(matches, key=lambda e: e.created_at)[-1]
        if entry.status != APPROVED:
            return None, f"run {entry.run_id[:8]} is {entry.status!r}, not approved", True
    else:
        approved = [e for e in entries if e.status == APPROVED]
        if not approved:
            return None, "no approved run in the ledger yet", False
        entry = sorted(approved, key=lambda e: e.created_at)[-1]
    if entry.blocklist_hits and not include_unsafe:
        return (
            None,
            f"run {entry.run_id[:8]} is approved but carries blocklist hits "
            f"({', '.join(entry.blocklist_hits)}); refusing to export without --include-unsafe",
            True,
        )
    return entry, "", False


def render_ad_copy_md(entry: LedgerEntry) -> str:
    ad = entry.ad_copy
    lines = [f"# Ad copy — {entry.run_id[:12]}",
             "",
             f"- mode: {entry.mode}",
             f"- exported at: {entry.created_at}",
             f"- decision: {entry.human_decision or 'n/a'}",
             ""]
    if ad is None:
        lines.append("(no ad copy recorded)")
        return "\n".join(lines) + "\n"
    lines += ["## Headline", "", ad.headline, "",
              "## Body", "", ad.body, "",
              "## Call to action", "", ad.cta, ""]
    if ad.disclosures:
        lines += ["## Disclosures", ""] + [f"- {d}" for d in ad.disclosures] + [""]
    return "\n".join(lines).rstrip("\n") + "\n"


def render_audit_report_md(entry: LedgerEntry) -> str:
    def _audit(name, audit):
        if audit is None:
            return [f"## {name} audit", "", "(not recorded)", ""]
        body = [f"## {name} audit", "",
                f"- approved: {audit.approved}",
                f"- risk score: {audit.risk_score}/10",
                f"- reviewed by human: {audit.reviewed_by_human}"]
        if audit.required_edits:
            body += ["- required edits:"] + [f"  - {e}" for e in audit.required_edits]
        if audit.reasons:
            body += ["- reasons:"] + [f"  - {r}" for r in audit.reasons]
        body.append("")
        return body

    hits = ", ".join(entry.blocklist_hits) if entry.blocklist_hits else "clean"
    lines = [f"# Audit report — {entry.run_id[:12]}", "",
             f"- run id: {entry.run_id}",
             f"- created: {entry.created_at}",
             f"- mode: {entry.mode}",
             f"- attempts: {entry.attempts}",
             f"- blocklist: {hits}",
             f"- human decision: {entry.human_decision or 'n/a'}",
             f"- error: {entry.error or 'none'}",
             f"- total latency: {entry.latency_ms_total:.1f} ms", ""]
    lines += _audit("Legal", entry.legal_result)
    lines += _audit("Brand", entry.brand_result)
    lines += ["## Stage history", "",
              f"| {'stage':<12} | {'try':>3} | {'status':<6} | {'model':<18} |",
              f"| {'-'*12} | {'-'*3} | {'-'*6} | {'-'*18} |"]
    for h in entry.history:
        lines.append(
            f"| {h.stage:<12} | {h.attempt:>3} | {h.status:<6} | {str(h.model or '-'):<18} |"
        )
    return "\n".join(lines) + "\n"


def export(entry: LedgerEntry, out_root: Path) -> Path:
    target = out_root / _sanitized_prefix(entry.run_id)
    if target.exists():
        raise FileExistsError(str(target))
    target.mkdir(parents=True, exist_ok=False)
    (target / "ad_copy.md").write_text(render_ad_copy_md(entry), encoding="utf-8")
    (target / "audit_report.md").write_text(render_audit_report_md(entry), encoding="utf-8")
    copy_json = entry.ad_copy.model_dump() if entry.ad_copy else {}
    (target / "ad_copy.json").write_text(
        json.dumps(copy_json, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return target


def main(argv=None) -> int:
    args = parse_args(argv)

    try:
        entries = read_entries(args.ledger)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    entry, message, refused = pick_run(entries, args.run_id, args.include_unsafe)
    if entry is None:
        print(("Refused: " if refused else "Error: ") + message, file=sys.stderr)
        return 2 if refused else 1

    try:
        target = export(entry, args.out)
    except FileExistsError:
        target = Path(args.out) / _sanitized_prefix(entry.run_id)
        print(f"Error: {target} already exists; refusing to overwrite", file=sys.stderr)
        return 3
    print(f"Exported {entry.run_id[:12]} to {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
