"""Phase 2 evaluation harness for the ad-safety checks.

Scores the pipeline's safety behaviour against the fixture ad sets, split into
two groups:

    tuned    -> tests/fixtures/{pass_ads,fail_ads}.json          (10 + 10)
    heldout  -> tests/fixtures/heldout/{pass_ads,fail_ads}.json  ( 5 +  5)

The heldout split is evaluation-only: it is NEVER used to tune prompts or the
blocklist (project rule 9). Results for the two splits are always reported
separately and never averaged together.

Two layers are evaluated:

  * blocklist (always): the deterministic hard floor. Free, no network. A pass
    ad with any hit is a false positive; a fail ad must be flagged and its hits
    must cover the fixture's `expected_blocklist_hits`.
  * audit (--live only): sends each ad through the real provider's legal audit
    prompt and adds the blocklist floor locally. This SPENDS REAL QUOTA -- one
    generateContent request per ad -- and is never run automatically.

Run from the project root:

    python scripts/eval_harness.py --split tuned
    python scripts/eval_harness.py --split heldout
    python scripts/eval_harness.py --split heldout --live
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.blocklist import check_blocklist  # noqa: E402
from src.config import QuotaState, Settings  # noqa: E402
from src.pipeline.nodes import DEFAULT_PROMPT_DIR, load_prompt  # noqa: E402
from src.schemas import AuditResult  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"

SPLITS = {
    "tuned": (FIXTURES / "pass_ads.json", FIXTURES / "fail_ads.json"),
    "heldout": (
        FIXTURES / "heldout" / "pass_ads.json",
        FIXTURES / "heldout" / "fail_ads.json",
    ),
}


def _load(path: Path) -> list[dict[str, Any]]:
    return json.loads(path.read_text(encoding="utf-8"))


def _ad_text(ad: dict[str, Any]) -> str:
    parts = [ad.get("headline", ""), ad.get("body", ""), ad.get("cta", "")]
    parts.extend(ad.get("disclosures", []))
    return "\n".join(parts)


def _truncate(text: str, width: int = 34) -> str:
    text = " ".join(text.split())
    return text if len(text) <= width else text[: width - 1] + "\u2026"


def load_dotenv(path: Path) -> None:
    """Minimal KEY=VALUE parser (mirrors scripts/smoke_test.py)."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


# ---------------------------------------------------------------------------
# Layer 1: deterministic blocklist
# ---------------------------------------------------------------------------

def evaluate_blocklist(
    pass_ads: list[dict[str, Any]],
    fail_ads: list[dict[str, Any]],
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    false_positives = 0
    false_negatives = 0
    expected_total = 0
    expected_found = 0

    for ad in pass_ads:
        hits = check_blocklist(_ad_text(ad))
        verdict = "ok" if not hits else "FALSE POSITIVE"
        if hits:
            false_positives += 1
        rows.append(
            {
                "kind": "pass",
                "headline": ad.get("headline", ""),
                "hits": hits,
                "verdict": verdict,
            }
        )

    for raw in fail_ads:
        ad = raw["ad"]
        expected = raw.get("expected_blocklist_hits", [])
        hits = check_blocklist(_ad_text(ad))
        hit_set = set(hits)
        expected_total += len(expected)
        expected_found += len(set(expected) & hit_set)
        if not hits:
            verdict = "FALSE NEGATIVE"
            false_negatives += 1
        elif set(expected) <= hit_set:
            verdict = "detected"
        else:
            verdict = "partial"
        rows.append(
            {
                "kind": "fail",
                "headline": ad.get("headline", ""),
                "hits": hits,
                "verdict": verdict,
            }
        )

    total = len(rows)
    correct = sum(1 for r in rows if r["verdict"] in ("ok", "detected"))
    recall = (expected_found / expected_total) if expected_total else 1.0
    return {
        "rows": rows,
        "total": total,
        "correct": correct,
        "false_positives": false_positives,
        "false_negatives": false_negatives,
        "expected_hit_recall": recall,
    }


# ---------------------------------------------------------------------------
# Layer 2: live audit (optional)
# ---------------------------------------------------------------------------

def evaluate_audit_live(
    pass_ads: list[dict[str, Any]],
    fail_ads: list[dict[str, Any]],
) -> dict[str, Any]:
    load_dotenv(ROOT / ".env")
    from src.llm import call_llm  # imported lazily; only needed for --live

    settings = Settings.from_env()
    quota = QuotaState(daily_quota=settings.daily_quota)
    if not settings.api_key:
        raise SystemExit("No API key found. Set LLM_API_KEY in .env for --live.")
    system = load_prompt("legal_audit", DEFAULT_PROMPT_DIR)

    def decide(ad: dict[str, Any]) -> tuple[bool, list[str]]:
        user = f"Ad copy:\n{json.dumps(ad, ensure_ascii=False, indent=2)}"
        result = call_llm(
            "audit",
            system,
            user,
            json_mode=True,
            response_model=AuditResult,
            settings=settings,
            quota=quota,
        )
        if not isinstance(result, AuditResult):
            result = AuditResult.model_validate(result)
        hits = check_blocklist(_ad_text(ad))
        approved = bool(result.approved) and not hits
        return approved, hits

    rows: list[dict[str, Any]] = []
    correct = 0
    for ad in pass_ads:
        approved, hits = decide(ad)
        expected_approved = True
        ok = approved == expected_approved
        correct += int(ok)
        rows.append(
            {
                "kind": "pass",
                "headline": ad.get("headline", ""),
                "approved": approved,
                "hits": hits,
                "verdict": "ok" if ok else "FALSE POSITIVE",
            }
        )
    for raw in fail_ads:
        ad = raw["ad"]
        approved, hits = decide(ad)
        expected_approved = False
        ok = approved == expected_approved
        correct += int(ok)
        rows.append(
            {
                "kind": "fail",
                "headline": ad.get("headline", ""),
                "approved": approved,
                "hits": hits,
                "verdict": "ok" if ok else "FALSE NEGATIVE",
            }
        )

    total = len(rows)
    return {
        "rows": rows,
        "total": total,
        "correct": correct,
        "accuracy": (correct / total) if total else 1.0,
        "requests": total,
        "quota_used": quota.used,
    }


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def print_blocklist_report(split: str, result: dict[str, Any]) -> None:
    print(f"\n=== Blocklist layer ({split}) ===")
    print(f"  {'kind':<5} {'headline':<34} verdict")
    for row in result["rows"]:
        print(
            f"  {row['kind']:<5} {_truncate(row['headline']):<34} {row['verdict']}"
        )
    print("  ---")
    print(f"  ads               : {result['total']}")
    print(f"  correct           : {result['correct']}/{result['total']}")
    print(f"  false positives   : {result['false_positives']}")
    print(f"  false negatives   : {result['false_negatives']}")
    print(f"  expected-hit recall: {result['expected_hit_recall'] * 100:.1f}%")


def print_audit_report(split: str, result: dict[str, Any]) -> None:
    print(f"\n=== Live audit layer ({split}) ===")
    print(f"  {'kind':<5} {'headline':<34} {'approved':<9} verdict")
    for row in result["rows"]:
        print(
            f"  {row['kind']:<5} {_truncate(row['headline']):<34} "
            f"{str(row['approved']):<9} {row['verdict']}"
        )
    print("  ---")
    print(f"  ads       : {result['total']}")
    print(f"  accuracy  : {result['accuracy'] * 100:.1f}%")
    print(f"  requests  : {result['requests']} (quota used {result['quota_used']})")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate ad-safety behaviour.")
    parser.add_argument(
        "--split",
        choices=sorted(SPLITS),
        default="tuned",
        help="fixture split to score",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="also run the real legal audit (spends one request per ad)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    pass_path, fail_path = SPLITS[args.split]
    pass_ads = _load(pass_path)
    fail_ads = _load(fail_path)

    print(f"Eval harness -- split={args.split} ({'live' if args.live else 'offline'})")

    blocklist = evaluate_blocklist(pass_ads, fail_ads)
    print_blocklist_report(args.split, blocklist)

    if args.live:
        print(
            f"\n[--live] this run spends {len(pass_ads) + len(fail_ads)} real "
            f"requests (one per ad)."
        )
        audit = evaluate_audit_live(pass_ads, fail_ads)
        print_audit_report(args.split, audit)

    # Non-zero exit if the deterministic layer regressed at all.
    if blocklist["false_positives"] or blocklist["false_negatives"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
