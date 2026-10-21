# AI Creative Agency Automation

A small, safety-first pipeline that turns a messy client brief into ad copy,
then runs a deterministic blocklist plus legal and brand audits before a human
sign-off. Phase 1 (LLM wrapper, schemas, blocklist, prompts, tests) and the
live smoke test are complete; the LangGraph pipeline is Phase 2.

Testing uses fake fixtures only. No real client data belongs in this repo.

## Status

- Phase 1: config, provider-agnostic LLM wrapper, Pydantic schemas, blocklist,
  prompts, mocked test suite.
- Phase 1.5: live smoke test against the configured Gemini model (passed).
- Phase 2: LangGraph pipeline with a human approval gate (complete).

## Requirements

- Python 3.10+ (the original brief asked for 3.11; the code runs on 3.10).
- A Gemini API key in the environment for live calls. Tests never call out.

> **Python version note:** this checkout is currently developed and tested on
> **Python 3.10.6**. Upgrading to **3.11+** is planned before Phase 3 begins.

```powershell
python -m pip install -r requirements.txt
```

## Configuration

All settings come from environment variables; `.env.example` lists them with
blank/TODO values. Copy it to `.env` and fill in your own values. `.env` is
gitignored and must never be committed.

| Variable | Meaning |
| --- | --- |
| `LLM_PROVIDER` | Provider id. Only `gemini` is implemented. |
| `LLM_API_KEY` / `GEMINI_API_KEY` | API key. Read from the environment only. |
| `LLM_MODEL` | Shared model id for every stage (bare id, e.g. `gemini-3.8-flash`). No default is shipped on purpose. |
| `LLM_MODEL_INTAKE`, `LLM_MODEL_CREATIVE`, `LLM_MODEL_AUDIT`, `LLM_MODEL_BRAND_AUDIT` | Per-stage model overrides. |
| `LLM_TIMEOUT_SECONDS` | HTTP read timeout per attempt. |
| `LLM_MAX_RETRIES` | **Maximum total attempts per call, including the initial attempt.** Hard cap is 3. `1` means a single attempt with no retries; `3` means one initial attempt plus up to two retries. |
| `LLM_RETRY_BACKOFF_BASE` | Exponential backoff base for retryable failures (429, 5xx, timeouts). |
| `LLM_DAILY_QUOTA` | Local daily request budget. |
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
