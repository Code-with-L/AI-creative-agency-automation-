# Project Plan — AI Creative Agency Automation

Working plan rebuilt from the repository itself (the original reference was
lost). Phases 1–5 are built and tested; see the risk table for what is still open.

## Ground rules (unchanged)

- Testing uses fake fixtures only; no real client data anywhere in the repo.
- Free tier is for testing; a live run spends roughly 4–8 requests from a
  ~50/day budget that is currently **exhausted**. Live verification of any
  change is blocked until quota resets.
- No pushing, no deploying. Commits stay local until explicitly asked.
- A human always approves; the deterministic blocklist is the floor; the
  legal audit is a first-pass filter only.
- Heldout fixtures (`tests/fixtures/heldout/`) are never used for tuning.

## Phase 1 — Schemas, config, LLM wrapper, blocklist, prompts (DONE)

Pydantic models, provider-agnostic Gemini wrapper with 429/5xx/timeout retry
(backoff + daily quota guard, hard cap 3 retries), 16-term whole-word
blocklist, tuned prompts with enumerated `AuditResult` keys, fully mocked
test suite. In `018ca12`, plus unpushed working-tree hardening (prompt fixes,
retry cap enforcement).

## Phase 1.5 — Live smoke test (DONE, quota spent)

Verified the 503-retry path against the real provider (exactly 3 attempts).

## Phase 2 — Pipeline with human gate (DONE; live proof blocked)

LangGraph graph: intake → creative → blocklist → legal/brand audit → revise
loop (cap 3) → human gate → approve/reject/edit. Demo mode fully offline;
`--live` real-provider mode; `--edited-copy` to answer the gate with edited
copy; `--auto-decision` refused in live mode (exit 6).

**Open:** the edit path and the latest audit-prompt changes are only
test-verified; a live gated run is pending the quota reset.

## Phase 3 — Audit ledger (DONE, this commit)

`--ledger [PATH]` on the pipeline records every run (approved, rejected,
failed, aborted) to an append-only JSONL ledger: run id, mode, brief, final
copy, blocklist hits, audit results, human decision, per-stage latency.
`scripts/ledger_report.py` is a read-only report that flags approved runs
which still carry blocklist hits (exit 2). `.data/`/`*.jsonl` gitignored.

## Phase 4 — Deliverable export (DONE)

The product story ended at the human gate with no artifact for the client.
Phase 4 closes the loop: `scripts/export_deliverable.py` takes an **approved**
ledger entry and emits a deterministic deliverable package — copy as
markdown, audit report, and a machine-readable JSON copy.

- `--ledger`, `--run-id` (default: latest clean approved run), `--out`
  (default `.data/deliverables/`).
- Safety: refuses to export unapproved runs; refuses approved-with-blocklist-
  hits unless `--include-unsafe`; refuses to overwrite existing output (exit 3).
- Fully tested offline (`tests/test_export_deliverable.py`, 7 cases); no quota.

## Phase 5 — Distribution & CI (DONE)

- Implementation of the CLIs lives in `src/` (`cli.py`, `ledger_report.py`,
  `export_deliverable.py`); `scripts/*.py` are thin backward-compatible
  wrappers.
- `pyproject.toml`: setuptools backend, package `src` + `src.pipeline`,
  dependencies mirroring requirements.txt (kept in sync by comment),
  `dev` extra with pytest, entry points `ai-creative-pipeline`,
  `ai-creative-ledger`, `ai-creative-deliverable`. Editable install verified
  locally; entry points verified from a foreign cwd.
- `.github/workflows/ci.yml`: ruff lint, pytest, and tuned/heldout eval splits on Python
  3.10/3.11/3.12, plus a secret-scan job (no `.env` tracked; no `AIza...`
  strings in tracked files). The whole codebase is ruff-clean (F/I/UP).
  Never pushed without an explicit request.
- Python 3.11/3.12 only exercised in CI config; this checkout runs 3.10.6.

## Risks / state

| Item | Status |
| --- | --- |
| Quota exhausted; live verification blocked | retry after reset |
| Working tree uncommitted (10+ files, ~3 phases of changes) | local only; not pushed |
| Audit-prompt live anomaly (`passed`/`summary` shape) | prompt now enumerates keys; schema-validation failures retry within the hard cap; unverified live until quota resets |
| Daily quota guard was per-process (meaningless across runs) | fixed: persisted to `.data/quota.json` (atomic writes, day rollover, fail-safe warnings) |
| Python 3.11 absent in this checkout | blocks 3.11 verification |
| Legal audit schema: model emitted non-enumerated keys in an earlier run | Mitigated: schema-validation failures now retry within the hard cap of 3 (see `src/llm.py`); unverified live until quota resets |
