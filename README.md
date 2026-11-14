# AI Creative Agency Automation

[![ci](https://github.com/Code-with-L/AI-creative-agency-automation-/actions/workflows/ci.yml/badge.svg)](https://github.com/Code-with-L/AI-creative-agency-automation-/actions/workflows/ci.yml)
[![license](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)

A small, safety-first pipeline that turns a messy client brief into ad copy,
then runs a deterministic blocklist plus legal and brand audits before a human
sign-off. Phases 1 (core), 1.5 (live smoke test), 2 (pipeline + human gate),
3 (audit ledger), 4 (deliverable export) and 5 (packaging + CI) are complete.

Testing uses fake fixtures only. No real client data belongs in this repo.

## Status

- Phase 1: config, provider-agnostic LLM wrapper, Pydantic schemas, blocklist,
  prompts, mocked test suite.
- Phase 1.5: live smoke test against the configured Gemini model (passed).
- Phase 2: LangGraph pipeline with a human approval gate (complete).
- Phase 3: audit ledger (`--ledger`) and `scripts/ledger_report.py`, so each
  run leaves a tamper-evident JSONL record of the copy, the audits, the
  blocklist result and the human decision.
- Phase 4: deliverable export (`scripts/export_deliverable.py`) — packages an
  approved ledger run into a client-ready folder: copy as markdown, machine
  readable JSON, and an audit report. Exports unapproved or unsafe runs are
  refused by default.
- Phase 5: packaging (`pyproject.toml`, console entry points, editable
  install), lint (ruff: F/I/UP) and CI (`.github/workflows/ci.yml`): pytest +
  tuned/heldout eval on Python 3.10/3.11/3.12, plus a secret-scan job.

## Requirements

- Python 3.10+ (the original brief asked for 3.11; the code runs on 3.10).
- A Gemini API key in the environment for live calls. Tests never call out.

```powershell
python -m pip install -r requirements.txt
```

Or install the package (editable) with its entry points:

```powershell
python -m pip install -e .[dev]
```

which provides `ai-creative-pipeline`, `ai-creative-ledger` and
`ai-creative-deliverable`. The `scripts/*.py` files remain as thin wrappers.

> **Python version note:** this checkout is developed and tested on
> **Python 3.10.6**. CI runs the suite and both eval splits on 3.10, 3.11 and
> 3.12, but only 3.10 has been verified locally so far.

## Configuration

All settings come from environment variables; `.env.example` lists them with
blank/TODO values. Copy it to `.env` and fill in your own values. `.env` is
gitignored and must never be committed.

| Variable | Meaning |
| --- | --- |
| `LLM_PROVIDER` | Provider id: `gemini` (default) or `openai-compatible` (Groq, Together, OpenRouter, self-hosted shims). Anything else fails loudly. |
| `LLM_API_KEY` / `GEMINI_API_KEY` | API key. Read from the environment only. |
| `LLM_MODEL` | Shared model id for every stage (bare id, e.g. `gemini-3.8-flash`). No default is shipped on purpose. |
| `LLM_MODEL_INTAKE`, `LLM_MODEL_CREATIVE`, `LLM_MODEL_AUDIT`, `LLM_MODEL_BRAND_AUDIT` | Per-stage model overrides. |
| `LLM_TIMEOUT_SECONDS` | HTTP read timeout per attempt. |
| `LLM_MAX_RETRIES` | **Retries after the initial attempt.** Hard cap is 3 (so at most 4 attempts per call). `0` means a single attempt with no retries; `1` means one retry (2 attempts); `2` means two retries (3 attempts). Retries apply to 429, 5xx, timeouts and schema-validation failures, with exponential backoff. |
| `LLM_RETRY_BACKOFF_BASE` | Exponential backoff base for retryable failures (429, 5xx, timeouts, schema-validation). |
| `LLM_SAFETY_THRESHOLD` | HARM_CATEGORY_* threshold sent with every request (`BLOCK_MEDIUM_AND_ABOVE` default; `BLOCK_NONE` disables filtering). |
| `LLM_RESPONSE_SCHEMA` | Set to `1` to also send `generationConfig.responseSchema`, so the provider enforces the JSON shape server-side. Experimental until verified live. |
| `LLM_DAILY_QUOTA` | Local daily request budget. Persisted across runs to `.data/quota.json` so the budget is honoured between processes. |
| `LLM_QUOTA_STATE_PATH` | Where the daily counter is persisted (default `.data/quota.json`). |
| `LLM_ENFORCE_QUOTA` | Set to `0`/`false` to disable the local quota guard. |
| `LLM_ENDPOINT_TEMPLATE` | Override the generateContent URL template. |

Model ids are never hardcoded: a missing model fails loudly at call time
instead of guessing an id or endpoint. Provider rate limits and entitlements
change often, so confirm your own before running live.

## Tests

The suite is fully mocked (zero API cost, no network):

```powershell
python -m pytest -q
```

Heldout fixtures under `tests/fixtures/heldout/` are for evaluation only and
must never be used to tune prompts.

## Docker

A rootless-by-default image is provided; it contains no secrets and no local
state:

```powershell
docker build -t ai-creative-agency-automation .
docker run --rm --env-file .env ai-creative-agency-automation --help
```

It installs the package (not editable) and points `PROMPT_DIR` at the bundled
prompts. The `.env` file is never copied into the image (see `.dockerignore`).

## Web UI

A local single-page app answers the human gate in a browser instead of a
terminal — brief in, audited copy out, you approve/edit/reject with a click:

```powershell
python -m pip install -e .[web]
python -m src.webui        # or: ai-creative-agency
# open http://127.0.0.1:8000
```

Demo mode is the default (offline, no quota). A `live provider` toggle runs
the real pipeline and spends quota — a live run is never auto-approved; the
gate buttons are the only path forward. The page shows the copy, both audits,
blocklist status, full revise-loop history, and lets you edit the copy inline
before re-auditing. Safety model identical to the CLI: blocklist floor, audits,
and a human decision that the model never gets.

## Audit ledger

Every run can be recorded to a local JSONL ledger:

```powershell
python scripts/run_pipeline.py --auto-decision approve --ledger
python scripts/ledger_report.py
```

`--ledger` without a value writes to `.data/ledger.jsonl`; `--ledger PATH`
chooses another file. The ledger captures run id, mode, brief, final copy,
blocklist hits, both audit results, the human decision, per-stage history
with latency, and a total. It is an audit trail, not a database: entries are
append-only, and a corrupt line makes `read_entries` fail with `path:line`
instead of skipping the record.

The ledger contains client briefs and generated copy. It is gitignored
(`.data/`, `*.jsonl`) and must never be committed. `scripts/ledger_report.py`
is read-only and flags any approved run that still carries blocklist hits.

## Deliverable export

After a run is approved, package it for the client:

```powershell
python scripts/export_deliverable.py                 # latest clean approved run
python scripts/export_deliverable.py --run-id 5a4f431e
```

Artifacts land in `.data/deliverables/<run-id-prefix>/` (`ad_copy.md`,
`ad_copy.json`, `audit_report.md`). Safety rules: only `approved` runs are
exported, approved runs carrying blocklist hits are refused unless
`--include-unsafe` is passed, and an existing directory is never overwritten
(exit 3). The whole folder is gitignored with the rest of `.data/`.

## Live smoke test

`scripts/smoke_test.py` makes real requests against the configured provider,
so it consumes daily quota. It lists the account's models first, prints only
whether the key is set, and sends one intake request.

```powershell
python scripts/smoke_test.py
```

## Safety rules

- Free-tier access is for testing only; move to a paid tier by changing config,
  not code.
- The legal audit is a first-pass filter, not legal advice. Human approval and
  the deterministic blocklist always stay in the loop.
- Generated copy must not invent facts, numbers, claims, prices or deadlines.
- Keys come from the environment; never log, print or commit them.
