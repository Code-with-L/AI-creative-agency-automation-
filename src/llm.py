"""Provider-agnostic LLM wrapper (Phase 1).

Design goals:
* No hardcoded model names or endpoints (see src/config.py).
* Retries with exponential backoff, 429 handling, and a hard attempt cap.
* Optional JSON mode with Pydantic validation.
* A simple daily quota guard that fails gracefully instead of retrying forever.
* A trace hook recording stage, model, latency and status.
* Swappable provider: Gemini and an OpenAI-compatible chat-completions API
  (Groq, Together, OpenRouter, self-hosted shims, ...) are implemented;
  anything else raises explicitly so we never guess another provider's API.
"""
from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import requests
from pydantic import BaseModel, ValidationError

from . import config as config_mod
from .config import SAFETY_CATEGORIES, QuotaState, Settings
from .response_schema import to_gemini_response_schema

# Never sleep longer than this, even if the provider asks for it.
RETRY_AFTER_CAP_SECONDS = 30.0


class LLMError(RuntimeError):
    """Base class for all LLM wrapper failures."""


class QuotaExceeded(LLMError):
    """Raised when the local daily quota guard blocks a request."""


class ProviderError(LLMError):
    """Raised for provider-side failures that are not retryable."""


@dataclass
class Trace:
    stage: str
    model: str
    provider: str
    latency_ms: float
    status: str
    attempts: int
    error: str | None = None


TraceHook = Callable[[Trace], None]

# Default in-memory trace sink. Pass your own trace_hook, or clear this list.
traces: list[Trace] = []


def default_trace_hook(trace: Trace) -> None:
    traces.append(trace)


def _retry_after_seconds(headers: Any) -> float | None:
    """Parse a Retry-After header (integer seconds or HTTP-date), capped."""
    if not headers:
        return None
    try:
        items = list(headers.items())
    except AttributeError:
        return None
    lowered = {str(k).strip().lower(): v for k, v in items}
    raw = lowered.get("retry-after")
    if raw is None:
        return None
    raw = str(raw).strip()
    try:
        value = float(raw)
    except ValueError:
        try:
            from datetime import datetime, timezone
            from email.utils import parsedate_to_datetime

            value = (parsedate_to_datetime(raw) - datetime.now(timezone.utc)).total_seconds()
        except Exception:
            return None
    if value < 0:
        value = 0.0
    return min(value, RETRY_AFTER_CAP_SECONDS)


def _build_gemini_request(
    settings: Settings,
    model: str,
    system: str,
    user: str,
    json_mode: bool,
    response_model: type[BaseModel] | None,
) -> tuple[str, str, dict[str, str], dict[str, Any]]:
    url = settings.endpoint_template.format(model=model)
    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": settings.api_key or "",
    }
    body: dict[str, Any] = {
        "contents": [{"role": "user", "parts": [{"text": user}]}],
    }
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    body["safetySettings"] = [
        {"category": category, "threshold": settings.safety_threshold}
        for category in SAFETY_CATEGORIES
    ]
    if json_mode:
        body["generationConfig"] = {"responseMimeType": "application/json"}
        if settings.response_schema and response_model is not None:
            try:
                body["generationConfig"]["responseSchema"] = to_gemini_response_schema(
                    response_model
                )
            except ValueError as exc:
                raise ProviderError(str(exc)) from exc
    return model, url, headers, body


def _build_openai_compatible_request(
    settings: Settings,
    model: str,
    system: str,
    user: str,
    json_mode: bool,
) -> tuple[str, str, dict[str, str], dict[str, Any]]:
    # Any OpenAI-compatible chat-completions endpoint works (Groq, Together,
    # OpenRouter, a self-hosted vLLM/OpenAI shim, ...). The endpoint URL must
    # be configured explicitly; the Gemini default is never reused.
    url = settings.endpoint_template.format(model=model)
    if "googleapis.com" in url:
        raise ProviderError(
            "provider 'openai-compatible' is configured with the Gemini endpoint; "
            "set LLM_ENDPOINT_TEMPLATE to your OpenAI-compatible chat-completions URL "
            "(e.g. https://api.groq.com/openai/v1/chat/completions)."
        )
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {settings.api_key or ''}",
    }
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": user})
    body: dict[str, Any] = {"model": model, "messages": messages}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    return model, url, headers, body


def _build_request(
    settings: Settings,
    stage: str,
    system: str,
    user: str,
    json_mode: bool,
    response_model: type[BaseModel] | None = None,
) -> tuple[str, str, dict[str, str], dict[str, Any]]:
    model = settings.model_for_stage(stage)

    if not model:
        raise LLMError(
            f"No model configured for stage {stage!r}. Set LLM_MODEL (shared) "
            f"or LLM_MODEL_{stage.upper()} (per-stage)."
        )

    if settings.provider == "gemini":
        return _build_gemini_request(settings, model, system, user, json_mode, response_model)
    if settings.provider == "openai-compatible":
        return _build_openai_compatible_request(settings, model, system, user, json_mode)
    raise ProviderError(
        f"Provider {settings.provider!r} is not implemented yet. "
        "Supported: 'gemini', 'openai-compatible'."
    )


def _extract_text(settings: Settings, payload: Any) -> str:
    if settings.provider == "gemini":
        try:
            parts = payload["candidates"][0]["content"]["parts"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f"Unexpected provider response shape: {exc}") from exc
        text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
    elif settings.provider == "openai-compatible":
        try:
            text = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f"Unexpected provider response shape: {exc}") from exc
        text = str(text or "")
    else:
        raise ProviderError(
            f"Provider {settings.provider!r} is not implemented yet. "
            "Supported: 'gemini', 'openai-compatible'."
        )
    if not text:
        raise ProviderError("Provider returned no text content.")
    return text


def _parse_and_validate(text: str, json_mode: bool, response_model: type[BaseModel] | None) -> Any:
    if not json_mode:
        return text
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LLMError(f"Provider did not return valid JSON: {exc}") from exc
    if response_model is not None:
        try:
            return response_model.model_validate(data)
        except ValidationError as exc:
            raise LLMError(
                f"JSON did not match {response_model.__name__}: {exc}"
            ) from exc
    return data


def call_llm(
    stage: str,
    system: str,
    user: str,
    json_mode: bool = False,
    *,
    response_model: type[BaseModel] | None = None,
    settings: Settings | None = None,
    quota: QuotaState | None = None,
    http_post: Callable[..., Any] | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    trace_hook: TraceHook | None = None,
) -> Any:
    """Call the configured LLM once, with retries/quota, and return the result.

    Returns raw text, a parsed JSON object, or a validated `response_model`
    instance depending on the arguments.
    """
    active_settings = settings or config_mod.settings
    active_quota = quota if quota is not None else config_mod.quota
    poster = http_post or requests.post
    hook = trace_hook or default_trace_hook

    if stage not in config_mod.STAGES:
        raise LLMError(f"Unknown stage {stage!r}; expected one of {config_mod.STAGES}")

    if active_settings.provider not in ("gemini", "openai-compatible"):
        raise ProviderError(
            f"Provider {active_settings.provider!r} is not implemented yet. "
            "Supported: 'gemini', 'openai-compatible'."
        )

    if not active_settings.api_key:
        raise LLMError("No API key configured (set LLM_API_KEY).")

    model, url, headers, body = _build_request(active_settings, stage, system, user, json_mode, response_model)

    max_attempts = max(1, active_settings.max_retries + 1)
    started = time.perf_counter()
    last_error: str | None = None
    attempt = 0

    for attempt in range(1, max_attempts + 1):
        if active_settings.enforce_quota and not active_quota.has_capacity():
            elapsed = (time.perf_counter() - started) * 1000
            hook(Trace(stage, model, active_settings.provider, elapsed,
                       "quota_exceeded", attempt, "daily quota exhausted"))
            raise QuotaExceeded("Daily request quota reached; failing gracefully.")

        status_code: int | None = None
        retry_after = None
        try:
            response = poster(
                url, headers=headers, json=body, timeout=active_settings.timeout_seconds
            )
        except requests.exceptions.Timeout as exc:
            last_error = f"timeout: {exc}"
        except requests.exceptions.RequestException as exc:
            last_error = f"request error: {exc}"
        else:
            if active_settings.enforce_quota:
                active_quota.consume(1)
            status_code = response.status_code

            snippet = (getattr(response, "text", "") or "").strip()[:200]
            if status_code == 429:
                last_error = f"429 rate limited: {snippet}".strip()
                retry_after = _retry_after_seconds(getattr(response, "headers", None))
            elif 500 <= status_code < 600:
                last_error = f"server error {status_code}: {snippet}".strip()
                retry_after = _retry_after_seconds(getattr(response, "headers", None))
            elif status_code >= 400:
                elapsed = (time.perf_counter() - started) * 1000
                hook(Trace(stage, model, active_settings.provider, elapsed,
                           "error", attempt, f"HTTP {status_code}"))
                raise ProviderError(
                    f"Provider rejected request with HTTP {status_code}: "
                    f"{getattr(response, 'text', '')[:200]}"
                )
            else:
                try:
                    text = _extract_text(active_settings, response.json())
                    result = _parse_and_validate(text, json_mode, response_model)
                except LLMError as exc:
                    elapsed = (time.perf_counter() - started) * 1000
                    hook(Trace(stage, model, active_settings.provider, elapsed,
                               "error", attempt, str(exc)))
                    # Schema violations retry within the same hard cap: the
                    # model is stochastic and may produce valid output on a
                    # retry, but if it never does the run still fails fast.
                    last_error = str(exc)
                else:
                    elapsed = (time.perf_counter() - started) * 1000
                    hook(Trace(stage, model, active_settings.provider, elapsed,
                               "ok", attempt, None))
                    return result

        # Retryable failure: honour Retry-After when present, else back off.
        if attempt < max_attempts:
            if retry_after is not None:
                sleep_fn(retry_after)
            else:
                sleep_fn(active_settings.retry_backoff_base ** (attempt - 1))

    elapsed = (time.perf_counter() - started) * 1000
    hook(Trace(stage, model, active_settings.provider, elapsed,
               "error", attempt, last_error))
    raise LLMError(f"LLM call failed after {max_attempts} attempts: {last_error}")
