"""The creative pipeline CLI.

The importable implementation behind the ``ai-creative-pipeline`` console
entry point (see pyproject.toml) and the ``scripts/run_pipeline.py`` wrapper.

Drives the whole creative pipeline:

    intake -> creative -> blocklist -> legal audit -> brand audit
           -> (revise loop) -> human gate -> done

Two modes:

  * demo (the default): a deterministic, cost-free stub LLM. Runs fully
    offline, demonstrates the blocklist -> revise loop, and pauses at the
    human gate for your decision.
  * --live: uses the real provider through ``PipelineDeps.from_env()``. This
    SPENDS REAL QUOTA -- roughly 4-8 generateContent requests per full run out
    of the 50/day free budget. It is never run automatically.

The API key is only ever read from the environment and is never printed. Run
from the project root:

    python scripts/run_pipeline.py
    python scripts/run_pipeline.py --raw "Sell our cold brew to students."
    python scripts/run_pipeline.py --auto-decision approve
    python scripts/run_pipeline.py --live --raw "..."

The same commands are available as ``ai-creative-pipeline`` when the package
is installed (see pyproject.toml).

``--auto-decision`` is test-mode only: combining it with ``--live`` is refused
(exit 6), because a live approval must come from a human.

Decision prompt, when paused at the human gate:

    [a]pprove / [e]dit / [r]eject / [q]uit

``--edited-copy FILE`` supplies the replacement ``AdCopy`` JSON used when the
answer is "edit"; without it the demo fixture copy is submitted.

``--ledger [PATH]`` appends a JSONL audit record of the run to the ledger
(default ``.data/ledger.jsonl``). The ledger contains client data and must
stay gitignored. Use ``scripts/ledger_report.py`` to review it.
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

from langgraph.types import Command  # noqa: E402  (after sys.path setup)

from src.config import QuotaState, Settings  # noqa: E402
from src.ledger import DEFAULT_LEDGER_PATH, append_entry, build_entry  # noqa: E402
from src.pipeline.graph import build_graph, new_thread_config  # noqa: E402
from src.pipeline.nodes import PipelineDeps  # noqa: E402
from src.pipeline.state import (  # noqa: E402
    APPROVED,
    AWAITING_HUMAN,
    ESCALATED,
    FAILED,
    REJECTED,
    HistoryEntry,
)
from src.schemas import AdCopy, AuditResult, CreativeBrief  # noqa: E402

MAX_GATES = 20
MAX_GATES_LIVE = 5
AUTO_APPROVED_LABEL = "AUTO_APPROVED (test mode)"
_FALLBACK_VERSION = "0.8.0"


def _version_string() -> str:
    try:
        from importlib.metadata import version

        return version("ai-creative-agency-automation")
    except Exception:
        return _FALLBACK_VERSION


def max_gates(live: bool) -> int:
    """Gate cap: tighter for live runs, looser for mocked demo runs."""
    return MAX_GATES_LIVE if live else MAX_GATES

DEFAULT_RAW_INPUT = (
    "Client: Northwind Coffee. Product: a monthly single-origin subscription. "
    "Audience: home espresso drinkers who want better beans without the fuss. "
    "Goal: drive subscriptions. Tone: warm, plain-spoken, no hype. "
    "Channels: email and instagram. Must include: 'cancel anytime'. "
    "Avoid: health claims and anything that sounds like a guarantee."
)


def load_dotenv(path: Path) -> None:
    """Minimal KEY=VALUE parser (mirrors scripts/smoke_test.py).

    Only used in --live mode. Existing environment variables always win.
    """
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


def load_edited_copy(path: str) -> AdCopy:
    """Read an ``AdCopy`` JSON file used to answer the human gate with "edit"."""
    file_path = Path(path)
    if not file_path.is_file():
        raise SystemExit(f"--edited-copy file not found: {file_path}")
    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"--edited-copy is not valid JSON ({file_path}): {exc}")
    if not isinstance(data, dict):
        raise SystemExit(f"--edited-copy must be a JSON object: {file_path}")
    try:
        return AdCopy.model_validate(data)
    except Exception as exc:  # pydantic ValidationError
        raise SystemExit(f"--edited-copy does not match AdCopy: {exc}")


# ---------------------------------------------------------------------------
# Demo (offline, cost-free) LLM stub
# ---------------------------------------------------------------------------

DEMO_BRIEF = CreativeBrief(
    client="Northwind Coffee",
    product="Monthly single-origin subscription",
    audience="Home espresso drinkers who want better beans without the fuss",
    goal="Drive subscriptions",
    tone="Warm, plain-spoken, no hype",
    channels=["email", "instagram"],
    must_include=["cancel anytime"],
    avoid=["health claims", "guarantees"],
)

DEMO_DIRTY_COPY = AdCopy(
    headline="Guaranteed results: barista-level coffee at home",
    body=(
        "Our subscription guarantees cafe-quality espresso, or your money back. "
        "Cancel anytime."
    ),
    cta="Subscribe now",
    disclosures=[],
)

DEMO_CLEAN_COPY = AdCopy(
    headline="Fresh roast, delivered on your schedule",
    body=(
        "Single-origin beans roasted the week they ship. Cancel anytime."
    ),
    cta="Order a bag",
    disclosures=[],
)

DEMO_AUDIT_OK = AuditResult(
    approved=True,
    risk_score=2,
    required_edits=[],
    reasons=["No claim issues found."],
    blocklist_hits=[],
)

# Marks the revise pass in `nodes._creative_user`.
_REVISE_MARKER = "Address every one of these required edits:"


def demo_llm(
    stage: str,
    system: str,
    user: str,
    *,
    json_mode: bool = False,
    response_model: type | None = None,
    settings: Settings | None = None,
    quota: QuotaState | None = None,
    trace_hook: Any | None = None,
) -> Any:
    """Deterministic stand-in for ``src.llm.call_llm``.

    Matches the call shape ``nodes._call`` uses and returns an instance of
    ``response_model``. The first creative pass is intentionally unsafe so the
    blocklist -> revise loop is visible; the revise pass is clean.
    """
    if response_model is CreativeBrief:
        raw_text = user.split("Brief:", 1)[-1].strip()
        return DEMO_BRIEF.model_copy(update={"raw_text": raw_text or None})
    if response_model is AdCopy:
        if _REVISE_MARKER in user:
            return DEMO_CLEAN_COPY
        return DEMO_DIRTY_COPY
    if response_model is AuditResult:
        # The hard blocklist floor in nodes._enforce_blocklist overrides this
        # for unsafe copy; for clean copy this "approve" stands.
        return DEMO_AUDIT_OK
    raise TypeError(f"demo_llm has no stub for response_model={response_model!r}")


def demo_deps() -> PipelineDeps:
    settings = Settings(
        api_key="demo",
        model_intake="demo-model",
        model_creative="demo-model",
        model_audit="demo-model",
        model_brand_audit="demo-model",
        timeout_seconds=1.0,
        retry_backoff_base=0.0,
        daily_quota=100,
        enforce_quota=False,
    )
    return PipelineDeps(
        settings=settings,
        quota=QuotaState(daily_quota=100),
        llm=demo_llm,
    )


def live_deps() -> PipelineDeps:
    env_path = ROOT / ".env"
    if not env_path.is_file():
        env_path = Path.cwd() / ".env"
    load_dotenv(env_path)
    deps = PipelineDeps.from_env()
    print("\n[--live] real provider mode")
    print(f"  provider : {deps.settings.provider}")
    print(f"  api key  : {'set' if deps.settings.api_key else 'MISSING'}")
    print(f"  model    : {deps.settings.model_for_stage('creative') or 'MISSING'}")
    print(
        f"  quota    : {deps.settings.daily_quota}/day "
        f"(enforce={deps.settings.enforce_quota})"
    )
    print("  WARNING: this run spends roughly 4-8 real requests.")
    if not deps.settings.api_key:
        raise SystemExit("No API key found. Set LLM_API_KEY in .env for --live.")
    return deps


# ---------------------------------------------------------------------------
# Decision handling
# ---------------------------------------------------------------------------

def _edited_payload(edited_copy: AdCopy | None) -> dict[str, Any]:
    """Serialize the copy submitted on "edit"; falls back to the demo fixture."""
    copy = edited_copy if edited_copy is not None else DEMO_CLEAN_COPY
    return copy.model_dump()


def prompt_decision(
    auto: str | None, edited_copy: AdCopy | None = None
) -> dict[str, Any] | None:
    """Return a resume payload dict, or None to quit."""
    if auto == "approve":
        return {"decision": "approve"}
    if auto == "reject":
        return {"decision": "reject"}
    if auto == "edit":
        return {"decision": "edit", "edited_copy": _edited_payload(edited_copy)}

    try:
        answer = input("Decision [a]pprove/[e]dit/[r]eject/[q]uit: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    if answer in ("a", "approve", ""):
        return {"decision": "approve"}
    if answer in ("r", "reject"):
        return {"decision": "reject"}
    if answer in ("e", "edit"):
        return {"decision": "edit", "edited_copy": _edited_payload(edited_copy)}
    if answer in ("q", "quit"):
        return None
    print("  Unrecognised choice; treating as reject.")
    return {"decision": "reject"}


def print_interrupt(state: dict[str, Any]) -> None:
    interrupts = state.get("__interrupt__") or []
    if not interrupts:
        return
    payload = getattr(interrupts[0], "value", interrupts[0])
    print("\n--- Human approval required ---")
    ad = payload.get("ad_copy") or {}
    print(f"  headline : {ad.get('headline', '(none)')}")
    print(f"  body     : {ad.get('body', '(none)')}")
    print(f"  cta      : {ad.get('cta', '(none)')}")
    legal = payload.get("legal_audit") or {}
    brand = payload.get("brand_audit") or {}
    print(
        f"  legal    : approved={legal.get('approved')} "
        f"risk={legal.get('risk_score')}"
    )
    print(
        f"  brand    : approved={brand.get('approved')} "
        f"risk={brand.get('risk_score')}"
    )
    hits = payload.get("blocklist_hits") or []
    print(f"  blocklist: {', '.join(hits) if hits else 'clean'}")
    print(f"  attempt  : {payload.get('attempt')}")


def record_auto_decision(state: dict[str, Any], decision: str) -> None:
    """Record that an automated test-mode decision was applied."""
    history = list(state.get("history") or [])
    history.append(
        HistoryEntry(
            stage="auto_decision",
            model=None,
            attempt=state.get("attempt", 0),
            status="ok",
            detail=f"test mode: {decision}",
        )
    )
    state["history"] = history


def display_status(state: dict[str, Any], auto_decision: str | None) -> Any:
    """Never present an automated approval as a plain human APPROVED."""
    status = state.get("status")
    if auto_decision and status == APPROVED:
        return AUTO_APPROVED_LABEL
    return status


def print_summary(state: dict[str, Any], auto_decision: str | None = None) -> None:
    print("\n--- Result ---")
    print(f"  status   : {display_status(state, auto_decision)}")
    print(f"  attempts : {state.get('attempt')}")
    hits = state.get("blocklist_hits") or []
    print(f"  blocklist: {', '.join(hits) if hits else 'clean'}")
    error = state.get("error")
    if error:
        print(f"  error    : {error}")
    ad = state.get("ad_copy")
    if ad is not None:
        if hasattr(ad, "model_dump"):
            ad = ad.model_dump()
        print("  ad copy  :")
        print(f"    headline : {ad.get('headline')}")
        print(f"    body     : {ad.get('body')}")
        print(f"    cta      : {ad.get('cta')}")

    print("\n--- History ---")
    print(f"  {'stage':<12} {'model':<14} {'try':>3}  {'status':<6} detail")
    for entry in state.get("history", []):
        if hasattr(entry, "model_dump"):
            entry = entry.model_dump()
        if not isinstance(entry, dict):
            entry = {}
        stage = entry.get("stage", "?")
        model = entry.get("model") or "-"
        attempt = entry.get("attempt", 0)
        status = entry.get("status", "?")
        detail = entry.get("detail") or ""
        print(f"  {stage:<12} {str(model):<14} {attempt:>3}  {status:<6} {detail}")


def write_ledger(state: dict[str, Any], args: argparse.Namespace) -> None:
    """Record the finished run in the audit ledger, if ``--ledger`` was given.

    A ledger failure never changes the exit code of the run — the pipeline
    already happened — but it is reported loudly on stderr because an audit
    trail that silently goes missing is worse than none at all.
    """
    if not args.ledger:
        return
    try:
        entry = build_entry(
            state,
            mode="live" if args.live else "demo",
            raw_input=args.raw,
        )
        ledger_path = append_entry(args.ledger, entry)
        print(f"\n  ledger   : recorded run {entry.run_id} to {ledger_path}")
    except Exception as exc:
        print(
            f"\n  warning  : could not write ledger ({exc}); audit trail NOT recorded",
            file=sys.stderr,
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the creative pipeline.")
    parser.add_argument("--version", action="version", version=_version_string())
    parser.add_argument(
        "--live",
        action="store_true",
        help="use the real provider (spends real quota); default is the demo stub",
    )
    parser.add_argument("--raw", default=DEFAULT_RAW_INPUT, help="raw client brief")
    parser.add_argument(
        "--auto-decision",
        choices=["approve", "edit", "reject"],
        help="answer the human gate non-interactively",
    )
    parser.add_argument("--thread", help="checkpoint thread id (default: random)")
    parser.add_argument(
        "--edited-copy",
        help="AdCopy JSON file submitted when the human gate is answered with edit",
    )
    parser.add_argument(
        "--ledger",
        nargs="?",
        const=DEFAULT_LEDGER_PATH,
        default=None,
        metavar="PATH",
        help="append this run to the audit ledger (JSONL). "
        "Runs without a value use .data/ledger.jsonl. "
        "The ledger contains client data and is gitignored.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.auto_decision and args.live:
        print(
            "Error: --auto-decision is test-mode only and cannot be combined with "
            "--live. A live approval must be given by a human.",
            file=sys.stderr,
        )
        return 6

    edited_copy = load_edited_copy(args.edited_copy) if args.edited_copy else None
    deps = live_deps() if args.live else demo_deps()

    mode = "live" if args.live else "demo"
    print(f"\nCreative pipeline ({mode} mode)")
    print(f"  raw input: {args.raw[:80]}{'...' if len(args.raw) > 80 else ''}")
    if edited_copy is not None:
        print(f"  edit copy: {args.edited_copy} (submitted when the gate is edited)")
    if args.auto_decision:
        print(f"  TEST MODE: automated decisions ({args.auto_decision}); not a human sign-off")

    app = build_graph(deps=deps)
    config = new_thread_config(args.thread)

    state = app.invoke({"raw_input": args.raw}, config)

    code: int
    if state.get("status") == FAILED:
        print_summary(state, args.auto_decision)
        code = 1
    else:
        gate_cap = max_gates(args.live)
        gate_count = 0
        aborted = False
        while state.get("__interrupt__"):
            gate_count += 1
            if gate_count > gate_cap:
                print(f"\nAborted: exceeded {gate_cap} human gates without finishing.")
                aborted = True
                break
            print_interrupt(state)
            # An automated decision is only applied to the first gate. Choosing
            # "edit" makes the graph re-audit and re-gate the edited copy, so blindly
            # replaying "edit" would loop forever; later gates auto-approve instead.
            auto = args.auto_decision if gate_count == 1 else "approve"
            if args.auto_decision and gate_count > 1:
                print(f"  (auto-decision: approving gate {gate_count} to avoid an edit loop)")
            payload = prompt_decision(auto, edited_copy)
            if payload is None:
                print("\nAborted at the human gate; no decision recorded.")
                aborted = True
                break
            state = app.invoke(Command(resume=payload), config)

        if aborted:
            code = 2
        else:
            if args.auto_decision:
                record_auto_decision(state, args.auto_decision)

            print_summary(state, args.auto_decision)
            if state.get("status") == APPROVED:
                if args.auto_decision:
                    print(f"\nFinal: {AUTO_APPROVED_LABEL} - automation, not a human sign-off.")
                else:
                    print("\nFinal: APPROVED by a human.")
                code = 0
            elif state.get("status") == REJECTED:
                if args.auto_decision:
                    print("\nFinal: REJECTED by automation (test mode).")
                else:
                    print("\nFinal: REJECTED by a human.")
                code = 3
            elif state.get("status") == ESCALATED:
                print("\nFinal: escalated to a human (no approval within the retry cap).")
                code = 4
            elif state.get("status") == AWAITING_HUMAN:
                print("\nFinal: still awaiting a human decision.")
                code = 5
            else:
                print(f"\nFinal: ended in status {state.get('status')!r}.")
                code = 0

    write_ledger(state, args)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
