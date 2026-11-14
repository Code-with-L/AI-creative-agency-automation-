"""Phase 1.5 live smoke test.

This is the ONLY place in the project that talks to the real provider. It:

  1. loads .env with a tiny built-in parser (no python-dotenv dependency),
  2. lists the models your key can see,
  3. makes ONE real JSON-mode call against a fake brief,
  4. prints status, model, latency, attempts and whether it parsed cleanly.

It never prints the API key or request headers. Run from the project root:

    python scripts/smoke_test.py

This spends one real generateContent request (plus one list call) of quota.
It is NOT run automatically and is NOT part of the mocked test suite.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import requests  # noqa: E402  (imported after sys.path is set up)

from src.config import QuotaState, Settings  # noqa: E402
from src.llm import LLMError, ProviderError, QuotaExceeded, call_llm  # noqa: E402
from src.schemas import CreativeBrief  # noqa: E402


def load_dotenv(path: Path) -> dict[str, str]:
    """Minimal KEY=VALUE parser. Existing environment variables always win."""
    parsed: dict[str, str] = {}
    if not path.is_file():
        return parsed
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
            parsed[key] = value
    return parsed


def redact(text: str, secret: str | None) -> str:
    """Strip the API key out of anything we print."""
    if secret:
        text = text.replace(secret, "***REDACTED***")
    return text


def list_models(settings: Settings) -> None:
    url = settings.list_models_url()
    print(f"\n[1/2] Listing models: GET {url}")
    try:
        response = requests.get(
            url,
            headers={"x-goog-api-key": settings.api_key or ""},
            timeout=settings.timeout_seconds,
        )
    except requests.exceptions.RequestException as exc:
        print(f"  ! list request failed: {redact(str(exc), settings.api_key)}")
        return
    if response.status_code != 200:
        print(
            f"  ! HTTP {response.status_code}: "
            f"{redact(response.text[:300], settings.api_key)}"
        )
        return
    models = response.json().get("models", [])
    print(f"  {len(models)} model(s) visible to this key:")
    for model in models:
        name = model.get("name", "?")
        methods = model.get("supportedGenerationMethods") or []
        suffix = "" if "generateContent" in methods else "  (no generateContent)"
        print(f"    - {name}{suffix}")
    if not models:
        print("  (none - check the key/project)")


def main() -> int:
    load_dotenv(ROOT / ".env")
    settings = Settings.from_env()

    print("Phase 1.5 live smoke test")
    print(f"  provider : {settings.provider}")
    print(f"  api key  : {'set' if settings.api_key else 'MISSING'}")
    print(f"  model    : {settings.model_for_stage('intake') or 'MISSING'}")
    print(f"  quota    : {settings.daily_quota}/day (enforce={settings.enforce_quota})")

    if not settings.api_key:
        print("\nNo API key found. Set LLM_API_KEY (or GEMINI_API_KEY) in .env.")
        return 2

    list_models(settings)

    if not settings.model_for_stage("intake"):
        print("\nNo model configured. Set LLM_MODEL in .env.")
        return 2

    print("\n[2/2] One real JSON-mode call (stage=intake)")
    briefs = json.loads(
        (ROOT / "tests" / "fixtures" / "briefs.json").read_text(encoding="utf-8")
    )
    brief = briefs[0]
    system = (ROOT / "prompts" / "intake.md").read_text(encoding="utf-8")
    user = brief.get("raw_text") or json.dumps(brief, indent=2)

    quota = QuotaState(settings.daily_quota)
    records = []
    started = time.perf_counter()
    try:
        result = call_llm(
            "intake", system, user, json_mode=True,
            response_model=CreativeBrief, settings=settings, quota=quota,
            trace_hook=records.append,
        )
    except QuotaExceeded as exc:
        print(f"  ! quota guard blocked the call: {exc}")
        return 3
    except ProviderError as exc:
        print(f"  ! provider error: {redact(str(exc), settings.api_key)}")
        return 4
    except LLMError as exc:
        print(f"  ! call failed: {redact(str(exc), settings.api_key)}")
        return 5

    latency_ms = (time.perf_counter() - started) * 1000.0
    attempts = records[-1].attempts if records else 1

    print("  status    : OK (parsed cleanly into CreativeBrief)")
    print(f"  model     : {settings.model_for_stage('intake')}")
    print(f"  attempts  : {attempts}")
    print(f"  latency   : {latency_ms:.0f} ms")
    print(f"  quota left: {quota.remaining()} of {settings.daily_quota}")
    print(f"  client    : {result.client}")
    print(f"  product   : {result.product}")
    print(f"  goal      : {result.goal}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
