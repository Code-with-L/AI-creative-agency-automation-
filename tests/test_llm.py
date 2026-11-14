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


def test_safety_settings_are_sent_with_every_request():
    captured = {}

    def poster(url, **kwargs):
        captured["json"] = kwargs["json"]
        return gemini_ok("hello")

    call_llm(
        "intake", SYSTEM, USER, settings=make_settings(), quota=QuotaState(100),
        http_post=poster, trace_hook=lambda t: None,
    )
    safety = captured["json"]["safetySettings"]
    categories = [entry["category"] for entry in safety]
    assert "HARM_CATEGORY_HARASSMENT" in categories
    assert "HARM_CATEGORY_HATE_SPEECH" in categories
    assert "HARM_CATEGORY_SEXUALLY_EXPLICIT" in categories
    assert "HARM_CATEGORY_DANGEROUS_CONTENT" in categories
    assert all(entry["threshold"] == "BLOCK_MEDIUM_AND_ABOVE" for entry in safety)


def test_invalid_safety_threshold_is_rejected():
    with pytest.raises(ValueError, match="safety_threshold"):
        make_settings(safety_threshold="BLOCK_ANYTHING_GOES")


def test_429_retry_after_header_is_honored_and_capped():
    class RetryAfterResponse(FakeResponse):
        def __init__(self, retry_after):
            super().__init__(429, text="rate limited")
            self.headers = {"Retry-After": str(retry_after)}

    slept = []
    calls = {"n": 0}

    def poster(url, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return RetryAfterResponse(120)
        return gemini_ok("recovered")

    result = call_llm(
        "audit", SYSTEM, USER, settings=make_settings(), quota=QuotaState(100),
        http_post=poster, sleep_fn=slept.append, trace_hook=lambda t: None,
    )
    assert result == "recovered"
    assert slept == [30.0]  # capped, not the 120s the provider asked for


def test_retry_after_header_below_cap_is_used_verbatim():
    class RetryAfterResponse(FakeResponse):
        def __init__(self, retry_after):
            super().__init__(429, text="rate limited")
            self.headers = {"retry-after": retry_after}

    slept = []

    def poster(url, **kwargs):
        if poster.calls == 0:  # first call only
            poster.calls += 1
            return RetryAfterResponse("7")
        return gemini_ok("recovered")

    poster.calls = 0
    result = call_llm(
        "audit", SYSTEM, USER, settings=make_settings(), quota=QuotaState(100),
        http_post=poster, sleep_fn=slept.append, trace_hook=lambda t: None,
    )
    assert result == "recovered"
    assert slept == [7.0]


def test_retry_after_garbage_falls_back_to_backoff():
    class GarbageRetryAfter(FakeResponse):
        def __init__(self):
            super().__init__(503, text="overloaded")
            self.headers = {"Retry-After": "soon"}

    slept = []

    def poster(url, **kwargs):
        if poster.n == 0:
            poster.n += 1
            return GarbageRetryAfter()
        return gemini_ok("recovered")

    poster.n = 0
    result = call_llm(
        "audit", SYSTEM, USER, settings=make_settings(), quota=QuotaState(100),
        http_post=poster, sleep_fn=slept.append, trace_hook=lambda t: None,
    )
    assert result == "recovered"
    assert slept == [1.0]  # default backoff base=2 -> 2**0


def test_response_schema_emitted_when_enabled():
    captured = {}

    def poster(url, **kwargs):
        captured["json"] = kwargs["json"]
        ad = {"headline": "H", "body": "B", "cta": "C", "disclosures": []}
        payload = {"candidates": [{"content": {"parts": [{"text": json.dumps(ad)}]}}]}
        return FakeResponse(200, payload=payload)

    result = call_llm(
        "creative", SYSTEM, USER, json_mode=True, response_model=AdCopy,
        settings=make_settings(response_schema=True), quota=QuotaState(100),
        http_post=poster, trace_hook=lambda t: None,
    )
    assert isinstance(result, AdCopy)
    schema = captured["json"]["generationConfig"]["responseSchema"]
    assert schema["type"] == "object"
    assert "headline" in schema["properties"]
    assert schema["required"] == ["headline", "body", "cta"]


def test_response_schema_absent_by_default():
    captured = {}

    def poster(url, **kwargs):
        captured["json"] = kwargs["json"]
        ad = {"headline": "H", "body": "B", "cta": "C", "disclosures": []}
        payload = {"candidates": [{"content": {"parts": [{"text": json.dumps(ad)}]}}]}
        return FakeResponse(200, payload=payload)

    call_llm(
        "creative", SYSTEM, USER, json_mode=True, response_model=AdCopy,
        settings=make_settings(), quota=QuotaState(100),
        http_post=poster, trace_hook=lambda t: None,
    )
    assert "responseSchema" not in captured["json"]["generationConfig"]


def test_unknown_provider_refuses_explicitly():
    with pytest.raises(ProviderError, match="not implemented"):
        call_llm("intake", SYSTEM, USER, settings=make_settings(provider="groq-native-thing"),
                 quota=QuotaState(100), http_post=lambda *a, **k: gemini_ok("x"),
                 trace_hook=lambda t: None)


def test_openai_compatible_request_shape_and_success():
    captured = {}

    def poster(url, **kwargs):
        captured["url"] = url
        captured["headers"] = kwargs["headers"]
        captured["json"] = kwargs["json"]
        payload = {"choices": [{"message": {"content": "open hello"}}]}
        return FakeResponse(200, payload=payload)

    settings = make_settings(
        provider="openai-compatible",
        endpoint_template="https://api.groq.com/openai/v1/chat/completions",
    )
    result = call_llm(
        "creative", SYSTEM, USER, settings=settings, quota=QuotaState(100),
        http_post=poster, trace_hook=lambda t: None,
    )
    assert result == "open hello"
    assert captured["url"] == "https://api.groq.com/openai/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer test-key"
    assert captured["json"]["model"] == "test-model"
    assert captured["json"]["messages"][0] == {"role": "system", "content": SYSTEM}
    assert captured["json"]["messages"][-1] == {"role": "user", "content": USER}
    assert "safetySettings" not in captured["json"]


def test_openai_compatible_json_mode_parses_into_model():
    ad = {"headline": "H", "body": "B", "cta": "C", "disclosures": []}

    def poster(url, **kwargs):
        assert kwargs["json"]["response_format"] == {"type": "json_object"}
        payload = {"choices": [{"message": {"content": json.dumps(ad)}}]}
        return FakeResponse(200, payload=payload)

    settings = make_settings(
        provider="openai-compatible",
        endpoint_template="https://api.groq.com/openai/v1/chat/completions",
    )
    result = call_llm(
        "creative", SYSTEM, USER, json_mode=True, response_model=AdCopy,
        settings=settings, quota=QuotaState(100), http_post=poster,
        trace_hook=lambda t: None,
    )
    assert isinstance(result, AdCopy)
    assert result.headline == "H"


def test_openai_compatible_refuses_gemini_endpoint():
    with pytest.raises(ProviderError, match="Gemini endpoint"):
        call_llm(
            "creative", SYSTEM, USER,
            settings=make_settings(provider="openai-compatible"),
            quota=QuotaState(100),
            http_post=lambda *a, **k: gemini_ok("x"),
            trace_hook=lambda t: None,
        )


def test_openai_compatible_4xx_fails_fast():
    def poster(url, **kwargs):
        return FakeResponse(401, text="unauthorized")

    with pytest.raises(ProviderError):
        call_llm(
            "creative", SYSTEM, USER,
            settings=make_settings(
                provider="openai-compatible",
                endpoint_template="https://api.groq.com/openai/v1/chat/completions",
            ),
            quota=QuotaState(100), http_post=poster,
            sleep_fn=lambda _s: None, trace_hook=lambda t: None,
        )


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


def test_503_then_success_retries_with_exponential_backoff():
    calls = {"n": 0}
    slept = []

    def poster(url, **kwargs):
        calls["n"] += 1
        if calls["n"] <= 2:
            return FakeResponse(503, text="model is experiencing high demand")
        return gemini_ok("recovered")

    records = []
    result = call_llm(
        "creative", SYSTEM, USER, settings=make_settings(max_retries=2),
        quota=QuotaState(100), http_post=poster, sleep_fn=slept.append,
        trace_hook=records.append,
    )
    assert result == "recovered"
    assert calls["n"] == 3
    assert slept == [1.0, 2.0]
    assert records[-1].status == "ok"
    assert records[-1].attempts == 3


def test_503_exhausted_raises_after_max_retries_plus_one_attempts():
    calls = {"n": 0}

    def poster(url, **kwargs):
        calls["n"] += 1
        return FakeResponse(503, text="model is experiencing high demand")

    records = []
    with pytest.raises(LLMError) as excinfo:
        call_llm(
            "creative", SYSTEM, USER, settings=make_settings(max_retries=2),
            quota=QuotaState(100), http_post=poster, sleep_fn=lambda _s: None,
            trace_hook=records.append,
        )
    assert calls["n"] == 3
    assert records[-1].attempts == 3
    assert "after 3 attempts" in str(excinfo.value)


def test_zero_retries_means_single_attempt():
    calls = {"n": 0}

    def poster(url, **kwargs):
        calls["n"] += 1
        return FakeResponse(503, text="model is experiencing high demand")

    with pytest.raises(LLMError):
        call_llm(
            "creative", SYSTEM, USER, settings=make_settings(max_retries=0),
            quota=QuotaState(100), http_post=poster, sleep_fn=lambda _s: None,
            trace_hook=lambda t: None,
        )
    assert calls["n"] == 1


def test_max_retries_above_cap_is_rejected():
    with pytest.raises(ValueError):
        make_settings(max_retries=4)


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


def _ad_payload(headline="H"):
    return {"headline": headline, "body": "B", "cta": "C", "disclosures": []}


def test_validation_failure_retries_then_recovers():
    calls = {"n": 0}

    def poster(url, **kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            # Not valid AdCopy: no cta/body on the first attempt.
            return gemini_ok(json.dumps({"headline": "H"}))
        return gemini_ok(json.dumps(_ad_payload()))

    slept = []
    result = call_llm(
        "creative", SYSTEM, USER, json_mode=True, response_model=AdCopy,
        settings=make_settings(), quota=QuotaState(100), http_post=poster,
        sleep_fn=slept.append, trace_hook=lambda t: None,
    )
    assert isinstance(result, AdCopy)
    assert calls["n"] == 3
    assert slept  # backoff between retries


def test_validation_failure_exhausts_cap_and_raises():
    calls = {"n": 0}

    def poster(url, **kwargs):
        calls["n"] += 1
        return gemini_ok(json.dumps({"not": "an ad"}))

    with pytest.raises(LLMError, match="failed after"):
        call_llm(
            "creative", SYSTEM, USER, json_mode=True, response_model=AdCopy,
            settings=make_settings(), quota=QuotaState(100), http_post=poster,
            sleep_fn=lambda _s: None, trace_hook=lambda t: None,
        )
    assert calls["n"] == make_settings().max_retries + 1


def test_validation_failure_consumes_quota_per_attempt():
    def poster(url, **kwargs):
        return gemini_ok(json.dumps({"headline": "H"}))

    quota = QuotaState(10)
    with pytest.raises(LLMError):
        call_llm(
            "creative", SYSTEM, USER, json_mode=True, response_model=AdCopy,
            settings=make_settings(), quota=quota, http_post=poster,
            sleep_fn=lambda _s: None, trace_hook=lambda t: None,
        )
    assert quota.used == make_settings().max_retries + 1


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
