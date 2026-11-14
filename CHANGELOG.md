# Changelog

## 0.6.0

### Added
- `generationConfig.responseSchema` support (opt-in via `LLM_RESPONSE_SCHEMA=1`):
  a checked pydantic→Gemini converter (`src/response_schema.py`) enforces the
  AdCopy/AuditResult/CreativeBrief JSON shape server-side; refuses loudly on
  anything it cannot translate.
- `HARM_CATEGORY_*` safety settings sent with every request; threshold
  configurable via `LLM_SAFETY_THRESHOLD` (`BLOCK_MEDIUM_AND_ABOVE` default).
- `Retry-After` header honoured on 429/5xx responses (integer seconds or
  HTTP-date), capped at 30s; falls back to exponential backoff when absent
  or unparseable.
- CLI: `--version`.
- Distribution: `pyproject.toml` with three console entry points, ruff lint
  (F/I/UP) wired into CI, GitHub Actions workflow (pytest + tuned/heldout eval
  on 3.10/3.11/3.12 + secret scan + pip-audit dependency audit).
- `SECURITY.md`, `LICENSE` (MIT), `docs/ARCHITECTURE.md`, `CHANGELOG.md`,
  dependabot config, PR and issue templates with a safety checklist.
- Cross-process daily quota guard (`.data/quota.json`) and schema-validation
  retries (both shipped in 0.5.0's working tree).

## 0.5.0

### Added
- Phase 3: append-only audit ledger (`src/ledger.py`) — one JSONL record per
  run with copy, blocklist hits, audit results, human decision, per-stage
  latency. `--ledger [PATH]` on the pipeline; `ai-creative-ledger` /
  `scripts/ledger_report.py` read-only report that flags approved runs with
  blocklist hits (exit 2).
- Phase 4: deliverable export (`src/export_deliverable.py`) — packages an
  approved run into `.data/deliverables/<id>/` (`ad_copy.md`, `ad_copy.json`,
  `audit_report.md`). Unapproved runs and unsafe approvals are refused;
  existing output directories are never overwritten.
- Phase 5: packaging (`pyproject.toml`, `ai-creative-pipeline`,
  `ai-creative-ledger`, `ai-creative-deliverable` entry points, editable
  install), ruff lint (F/I/UP) with a CI job, GitHub Actions workflow
  (pytest + both eval splits on 3.10/3.11/3.12 + secret scan).
- Cross-process daily quota guard: persisted to `.data/quota.json` (atomic
  writes, day rollover, fail-safe warnings; `LLM_QUOTA_STATE_PATH` to override).
- Schema-validation failures from the provider now retry within the hard cap
  of 3 attempts (previously killed the run with zero retries).

### Changed
- Implementations moved to `src/` (`cli.py`, `ledger_report.py`,
  `export_deliverable.py`); `scripts/*.py` remain as thin wrappers.
- `--edited-copy PATH` on the pipeline answers the human gate with a supplied
  `AdCopy` JSON (headline/CTA kept edit from the caller contract).

### Notes
- Free-tier quota was exhausted during the first live edit test (429s); the
  live gated path remains test-verified only until quota resets.
- Heldout fixtures are never used for tuning.
