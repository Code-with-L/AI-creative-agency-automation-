"""Phase 1 configuration loader.

All secrets and tunables come from environment variables. Nothing here
hardcodes an API key, and model names are always per-stage overridable so
that switching providers/models (or moving from a free tier to a paid tier)
is a config-only change.
"""
from __future__ import annotations

import json
import os
import warnings
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

# Stages supported by the wrapper. Keep in sync with the prompt files.
STAGES = ("intake", "creative", "audit", "brand_audit")

# Default provider endpoint, verified against the official Gemini API docs:
#   POST https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent
#   auth header: x-goog-api-key
# The {model} placeholder is filled from the per-stage model setting.
# HARM_CATEGORY_* enums documented by the Gemini generateContent API.
SAFETY_CATEGORIES = (
    "HARM_CATEGORY_HARASSMENT",
    "HARM_CATEGORY_HATE_SPEECH",
    "HARM_CATEGORY_SEXUALLY_EXPLICIT",
    "HARM_CATEGORY_DANGEROUS_CONTENT",
)
SAFETY_THRESHOLDS = (
    "HARM_BLOCK_THRESHOLD_UNSPECIFIED",
    "BLOCK_NONE",
    "BLOCK_ONLY_HIGH",
    "BLOCK_MEDIUM_AND_ABOVE",
    "BLOCK_LOW_AND_ABOVE",
)

DEFAULT_PROVIDER = "gemini"
DEFAULT_ENDPOINT_TEMPLATE = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)

# NOTE: we intentionally ship NO default model name. Model ids are deprecated
# often and entitlements differ per account, so a name must be supplied via
# LLM_MODEL (shared) or a per-stage override. A missing name fails loudly at
# call time rather than silently guessing. See project rule 3.
# TODO: confirm the exact model ids you are entitled to before going live.


def _env(name: str, default: str | None) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return default
    value = value.strip()
    return value or default


def _env_int(name: str, default: int) -> int:
    raw = _env(name, None)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"Environment variable {name} must be an integer, got {raw!r}")


def _env_float(name: str, default: float) -> float:
    raw = _env(name, None)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        raise ValueError(f"Environment variable {name} must be a number, got {raw!r}")


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name, None)
    if raw is None:
        return default
    return raw.lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    provider: str = DEFAULT_PROVIDER
    api_key: str | None = None
    endpoint_template: str = DEFAULT_ENDPOINT_TEMPLATE
    model_intake: str = ""
    model_creative: str = ""
    model_audit: str = ""
    model_brand_audit: str = ""
    # Hard cap on retries after the initial attempt (never infinite).
    # See project rule 6: at most 3 retries, i.e. 4 attempts.
    max_retries: int = 3
    timeout_seconds: float = 30.0
    retry_backoff_base: float = 2.0
    daily_quota: int = 50
    enforce_quota: bool = True
    # Where the cross-process daily counter is persisted. Default keeps the
    # budget honest between runs; override with LLM_QUOTA_STATE_PATH.
    quota_state_path: str | None = ".data/quota.json"
    # HARM_CATEGORY_* threshold sent with every request (Gemini docs).
    # BLOCK_NONE disables filtering entirely.
    safety_threshold: str = "BLOCK_MEDIUM_AND_ABOVE"
    # Opt-in: also emit generationConfig.responseSchema so the provider
    # enforces our JSON shape server-side. Experimental until verified live.
    response_schema: bool = False

    def __post_init__(self) -> None:
        if not 0 <= self.max_retries <= 3:
            raise ValueError(
                f"max_retries must be between 0 and 3 (got {self.max_retries}); "
                "retries are capped at 3 by project rule 6."
            )
        if self.safety_threshold not in SAFETY_THRESHOLDS:
            raise ValueError(
                f"safety_threshold must be one of {', '.join(SAFETY_THRESHOLDS)}; "
                f"got {self.safety_threshold!r}"
            )

    def model_for_stage(self, stage: str) -> str:
        mapping = {
            "intake": self.model_intake,
            "creative": self.model_creative,
            "audit": self.model_audit,
            "brand_audit": self.model_brand_audit,
        }
        try:
            return mapping[stage]
        except KeyError:
            raise KeyError(f"Unknown stage {stage!r}; expected one of {STAGES}")

    def list_models_url(self) -> str:
        """Derive the models.list endpoint from the generateContent template.

        For the default template this yields
        https://generativelanguage.googleapis.com/v1beta/models
        """
        marker = "/models/"
        if marker not in self.endpoint_template:
            raise ValueError(
                "Cannot derive the models.list URL from endpoint template "
                f"{self.endpoint_template!r}; expected it to contain '/models/'."
            )
        return self.endpoint_template.split(marker, 1)[0] + "/models"

    @classmethod
    def from_env(cls) -> Settings:
        # No hardcoded default: an empty name is carried through and rejected
        # with a clear error when a call is actually made (see src/llm.py).
        shared = _env("LLM_MODEL", "") or ""
        return cls(
            provider=_env("LLM_PROVIDER", DEFAULT_PROVIDER) or DEFAULT_PROVIDER,
            # Accept either the generic var or the provider-native one.
            api_key=_env("LLM_API_KEY", None) or _env("GEMINI_API_KEY", None),
            endpoint_template=_env("LLM_ENDPOINT_TEMPLATE", DEFAULT_ENDPOINT_TEMPLATE)
            or DEFAULT_ENDPOINT_TEMPLATE,
            model_intake=_env("LLM_MODEL_INTAKE", shared) or shared,
            model_creative=_env("LLM_MODEL_CREATIVE", shared) or shared,
            model_audit=_env("LLM_MODEL_AUDIT", shared) or shared,
            model_brand_audit=_env("LLM_MODEL_BRAND_AUDIT", shared) or shared,
            max_retries=_env_int("LLM_MAX_RETRIES", 3),
            timeout_seconds=_env_float("LLM_TIMEOUT_SECONDS", 30.0),
            retry_backoff_base=_env_float("LLM_RETRY_BACKOFF_BASE", 2.0),
            daily_quota=_env_int("LLM_DAILY_QUOTA", 50),
            enforce_quota=_env_bool("LLM_ENFORCE_QUOTA", True),
            quota_state_path=_env("LLM_QUOTA_STATE_PATH", ".data/quota.json"),
            safety_threshold=_env("LLM_SAFETY_THRESHOLD", "BLOCK_MEDIUM_AND_ABOVE")
            or "BLOCK_MEDIUM_AND_ABOVE",
            response_schema=_env_bool("LLM_RESPONSE_SCHEMA", False),
        )


@dataclass
class QuotaState:
    """Daily request counter with optional file persistence.

    Provider rate limits are per project and change often, so this is a simple,
    easily-reset guard rather than authoritative accounting. When ``path`` is
    set the counter is persisted as ``{"day": ..., "used": ...}`` on every
    consume, so the budget is honoured across processes, not just within one.

    Persistence failures never crash a run: an unreadable or unwritable quota
    file logs a warning and accounting falls back to in-memory.
    """

    daily_quota: int
    used: int = 0
    _day: date = field(default_factory=date.today)
    path: str | None = None

    def __post_init__(self) -> None:
        self._load()

    def _load(self) -> None:
        if not self.path:
            return
        state_path = Path(self.path)
        try:
            data = json.loads(state_path.read_text(encoding="utf-8"))
            saved_day = date.fromisoformat(str(data.get("day", "")))
            used = int(data.get("used", 0))
        except FileNotFoundError:
            return
        except Exception as exc:  # corrupt or unreadable file must not crash
            warnings.warn(f"Ignoring unreadable quota state {state_path}: {exc}")
            return
        if saved_day != date.today():
            self._day = date.today()
            self.used = 0
            return
        self._day = saved_day
        self.used = max(0, used)

    def _save(self) -> None:
        if not self.path:
            return
        state_path = Path(self.path)
        try:
            state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = state_path.with_name(state_path.name + ".tmp")
            tmp_path.write_text(
                json.dumps({"day": self._day.isoformat(), "used": self.used}),
                encoding="utf-8",
            )
            os.replace(tmp_path, state_path)
        except OSError as exc:
            warnings.warn(f"Could not persist quota state {state_path}: {exc}")

    def _rollover(self) -> None:
        today = date.today()
        if today != self._day:
            self._day = today
            self.used = 0

    def remaining(self) -> int:
        self._rollover()
        return max(0, self.daily_quota - self.used)

    def has_capacity(self) -> bool:
        return self.remaining() > 0

    def consume(self, n: int = 1) -> None:
        self._rollover()
        self.used += n
        self._save()


# Module-level singletons. `settings` is read from the environment at import
# time; tests construct their own Settings to stay hermetic.
settings = Settings.from_env()
quota = QuotaState(daily_quota=settings.daily_quota, path=settings.quota_state_path)
