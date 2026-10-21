"""LLM wrapper tests. Everything is mocked - zero cost, no network."""
from __future__ import annotations

import json

import pytest
import requests

from src.config import QuotaState, Settings
from src.llm import LLMError, ProviderError, QuotaExceeded, call_llm
from src.schemas import AdCopy

SYSTEM = "system prompt"
USER = "user prompt"


class FakeResponse:
    def __init__(self, status_code: int, payload=None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload


def gemini_ok(text: str) -> FakeResponse:
    payload = {"candidates": [{"content": {"parts": [{"text": text}]}}]}
    return FakeResponse(200, payload=payload, text=text)


def make_settings(**overrides) -> Settings:
    base = dict(
        provider="gemini",
        api_key="test-key",
        max_retries=3,
        timeout_seconds=5.0,
        retry_backoff_base=2.0,
        daily_quota=100,
        enforce_quota=True,
        model_intake="test-model",
        model_creative="test-model",
        model_audit="test-model",
        model_brand_audit="test-model",
    )
    base.update(overrides)
    return Settings(**base)


def test_text_mode_returns_text_and_uses_stage_model():
    captured = {}

    def poster(url, **kwargs):
        captured["url"] = url
        captured["headers"] = kwargs["headers"]
        captured["json"] = kwargs["json"]
        return gemini_ok("hello world")

    settings = make_settings()
    result = call_llm(
        "creative", SYSTEM, USER, settings=settings,
        quota=QuotaState(100), http_post=poster, trace_hook=lambda t: None,
    )
    assert result == "hello world"
    assert settings.model_for_stage("creative") in captured["url"]
    assert captured["headers"]["x-goog-api-key"] == "test-key"
    assert captured["json"]["contents"][0]["parts"][0]["text"] == USER


def test_json_mode_validates_into_pydantic_model():
    ad = {"headline": "H", "body": "B", "cta": "C", "disclosures": []}
    payload = {"candidates": [{"content": {"parts": [{"text": json.dumps(ad)}]}}]}

    def poster(url, **kwargs):
        assert kwargs["json"]["generationConfig"]["responseMimeType"] == "application/json"
        return FakeResponse(200, payload=payload)

    result = call_llm(
        "creative", SYSTEM, USER, json_mode=True, response_model=AdCopy,
        settings=make_settings(), quota=QuotaState(100), http_post=poster,
        trace_hook=lambda t: None,
    )
    assert isinstance(result, AdCopy)
    assert result.headline == "H"


def test_429_then_success_retries_with_backoff():
    calls = {"n": 0}
    slept = []

    def poster(url, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return FakeResponse(429, text="rate limited")
        return gemini_ok("recovered")

    result = call_llm(
        "audit", SYSTEM, USER, settings=make_settings(), quota=QuotaState(100),
        http_post=poster, sleep_fn=slept.append, trace_hook=lambda t: None,
    )
    assert result == "recovered"
    assert calls["n"] == 2
    assert slept  # backoff happened


def test_429_exhausted_raises_llm_error():
    def poster(url, **kwargs):
        return FakeResponse(429, text="rate limited")

    with pytest.raises(LLMError):
        call_llm(
            "audit", SYSTEM, USER, settings=make_settings(max_retries=3),
            quota=QuotaState(100), http_post=poster, sleep_fn=lambda _s: None,
            trace_hook=lambda t: None,
        )


def test_timeout_is_retried():
    calls = {"n": 0}

    def poster(url, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise requests.exceptions.Timeout("too slow")
        return gemini_ok("ok after timeout")

    result = call_llm(
        "intake", SYSTEM, USER, settings=make_settings(), quota=QuotaState(100),
        http_post=poster, sleep_fn=lambda _s: None, trace_hook=lambda t: None,
    )
    assert result == "ok after timeout"
    assert calls["n"] == 2


def test_non_retryable_4xx_raises_provider_error():
    def poster(url, **kwargs):
        return FakeResponse(400, text="bad request")

    with pytest.raises(ProviderError):
        call_llm(
            "intake", SYSTEM, USER, settings=make_settings(), quota=QuotaState(100),
            http_post=poster, sleep_fn=lambda _s: None, trace_hook=lambda t: None,
        )


def test_unknown_stage_raises():
    with pytest.raises(LLMError):
        call_llm("nope", SYSTEM, USER, settings=make_settings(),
                 quota=QuotaState(100), http_post=lambda *a, **k: gemini_ok("x"),
                 trace_hook=lambda t: None)


def test_missing_api_key_raises():
    with pytest.raises(LLMError):
        call_llm("intake", SYSTEM, USER, settings=make_settings(api_key=None),
                 quota=QuotaState(100), http_post=lambda *a, **k: gemini_ok("x"),
                 trace_hook=lambda t: None)


def test_quota_exceeded_fails_gracefully():
    def poster(url, **kwargs):
        raise AssertionError("must not call provider when quota is exhausted")

    with pytest.raises(QuotaExceeded):
        call_llm("intake", SYSTEM, USER, settings=make_settings(),
                 quota=QuotaState(0), http_post=poster, trace_hook=lambda t: None)


def test_trace_hook_records_status():
    records = []

    def poster(url, **kwargs):
        return gemini_ok("traced")

    call_llm("brand_audit", SYSTEM, USER, settings=make_settings(),
             quota=QuotaState(100), http_post=poster, trace_hook=records.append)
    assert len(records) == 1
    assert records[0].status == "ok"
    assert records[0].stage == "brand_audit"
    assert records[0].attempts == 1


def test_empty_model_raises_clear_error():
    with pytest.raises(LLMError) as excinfo:
        call_llm(
            "intake", SYSTEM, USER, settings=make_settings(model_intake=""),
            quota=QuotaState(100), http_post=lambda *a, **k: gemini_ok("x"),
            trace_hook=lambda t: None,
        )
    assert "LLM_MODEL" in str(excinfo.value)


def test_from_env_has_no_default_model_and_accepts_gemini_key(monkeypatch):
    for var in (
        "LLM_API_KEY", "GEMINI_API_KEY", "LLM_MODEL", "LLM_MODEL_INTAKE",
        "LLM_MODEL_CREATIVE", "LLM_MODEL_AUDIT", "LLM_MODEL_BRAND_AUDIT",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "gkey")
    monkeypatch.setenv("LLM_MODEL", "some-model")
    settings = Settings.from_env()
    assert settings.api_key == "gkey"
    assert settings.model_intake == "some-model"


def test_from_env_leaves_model_empty_when_unset(monkeypatch):
    for var in (
        "LLM_API_KEY", "GEMINI_API_KEY", "LLM_MODEL", "LLM_MODEL_INTAKE",
        "LLM_MODEL_CREATIVE", "LLM_MODEL_AUDIT", "LLM_MODEL_BRAND_AUDIT",
    ):
        monkeypatch.delenv(var, raising=False)
    assert Settings.from_env().model_for_stage("intake") == ""


def test_list_models_url_is_derived_from_template():
    settings = make_settings()
    assert settings.list_models_url() == (
        "https://generativelanguage.googleapis.com/v1beta/models"
    )
