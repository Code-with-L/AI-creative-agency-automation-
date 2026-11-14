"""Review the Phase 3 audit ledger.

Read-only report over the JSONL ledger produced by ``--ledger``:

    python scripts/ledger_report.py
    python scripts/ledger_report.py --ledger .data/ledger.jsonl
    python scripts/ledger_report.py --unsafe-only

Prints run counts by status, total and average latency, and flags any
``approved`` entry that still carries blocklist hits (an anomaly: the
deterministic blocklist is the safety floor). Contains client data; treat
the output like the input.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.ledger import (  # noqa: E402
    DEFAULT_LEDGER_PATH,
    LedgerEntry,
    read_entries,
    summarise,
    unsafe_approvals,
)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Report on the audit ledger.")
    parser.add_argument(
        "--ledger",
        type=Path,
        default=DEFAULT_LEDGER_PATH,
        help="path to the ledger JSONL (default: .data/ledger.jsonl)",
    )
    parser.add_argument(
        "--unsafe-only",
        action="store_true",
        help="only print entries approved despite blocklist hits",
    )
    return parser.parse_args(argv)


def _print_entry(entry: LedgerEntry) -> None:
    legal = entry.legal_result.risk_score if entry.legal_result else "-"
    brand = entry.brand_result.risk_score if entry.brand_result else "-"
    hits = ", ".join(entry.blocklist_hits) if entry.blocklist_hits else "clean"
    decision = entry.human_decision or "-"
    print(
        f"  {entry.created_at:<28} {entry.run_id[:8]:<8} {entry.mode:<4} "
        f"status={entry.status:<16} attempts={entry.attempts:<2} "
        f"legal={legal:<2} brand={brand:<2} blocklist={hits:<24} decision={decision}"
    )


def main(argv=None) -> int:
    args = parse_args(argv)

    try:
        entries = read_entries(args.ledger)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if not entries:
        print(f"No runs recorded in {args.ledger}")
        return 0

    if args.unsafe_only:
        flagged = unsafe_approvals(entries)
        if not flagged:
            print("No approved entries carry blocklist hits.")
            return 0
        print("Approved entries with blocklist hits (investigate):")
        for entry in flagged:
            _print_entry(entry)
        return 0

    print(f"Ledger: {args.ledger}")
    print(f"  runs    : {len(entries)}")
    counts = summarise(entries)
    for status in sorted(counts):
        print(f"    {status:<16} {counts[status]}")
    total_ms = sum(entry.latency_ms_total for entry in entries)
    avg_ms = total_ms / len(entries)
    print(f"  latency : {total_ms:.0f} ms total, {avg_ms:.0f} ms/run average")

    unsafe = unsafe_approvals(entries)
    if unsafe:
        print(f"\n  WARNING: {len(unsafe)} approved run(s) carry blocklist hits:")
        for entry in unsafe:
            _print_entry(entry)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
