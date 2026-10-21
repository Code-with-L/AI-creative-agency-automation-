"""Phase 1 configuration loader.

All secrets and tunables come from environment variables. Nothing here
hardcodes an API key, and model names are always per-stage overridable so
that switching providers/models (or moving from a free tier to a paid tier)
is a config-only change.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

# Stages supported by the wrapper. Keep in sync with the prompt files.
STAGES = ("intake", "creative", "audit", "brand_audit")

# Default provider endpoint, verified against the official Gemini API docs:
#   POST https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent
#   auth header: x-goog-api-key
# The {model} placeholder is filled from the per-stage model setting.
DEFAULT_PROVIDER = "gemini"
DEFAULT_ENDPOINT_TEMPLATE = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)

# NOTE: we intentionally ship NO default model name. Model ids are deprecated
# often and entitlements differ per account, so a name must be supplied via
# LLM_MODEL (shared) or a per-stage override. A missing name fails loudly at
# call time rather than silently guessing. See project rule 3.
# TODO: confirm the exact model ids you are entitled to before going live.


def _env(name: str, default: Optional[str]) -> Optional[str]:
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
    api_key: Optional[str] = None
    endpoint_template: str = DEFAULT_ENDPOINT_TEMPLATE
    model_intake: str = ""
    model_creative: str = ""
    model_audit: str = ""
    model_brand_audit: str = ""
    # Hard cap on total attempts per call (never infinite). See project rule 6.
    max_retries: int = 3
    timeout_seconds: float = 30.0
    retry_backoff_base: float = 2.0
    daily_quota: int = 50
    enforce_quota: bool = True

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
    def from_env(cls) -> "Settings":
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
        )


@dataclass
class QuotaState:
    """In-memory daily request counter.

    Provider rate limits are per project and change often, so this is a simple,
    easily-reset guard rather than authoritative accounting.
    """

    daily_quota: int
    used: int = 0
    _day: date = field(default_factory=date.today)

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


# Module-level singletons. `settings` is read from the environment at import
# time; tests construct their own Settings to stay hermetic.
settings = Settings.from_env()
quota = QuotaState(daily_quota=settings.daily_quota)
