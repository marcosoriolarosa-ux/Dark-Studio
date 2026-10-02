"""Tests for the AI gateway's structured-output handling.

Discovered while making the free model actually usable: every free model available
to a real key is a reasoning model, and they all prepend "Here's a thinking
process:" before the JSON, which broke the parser. The tier also returns
ResourceExhausted under load. These tests pin the fixes.
"""
import json
from unittest import mock

import httpx
import pytest

from backend.services import pipeline
from backend.services.auth_contract import (
    AUTH_MISSING_KEY,
    AUTH_MODEL_NOT_FREE,
    AUTH_QUOTA_EXCEEDED,
    AUTH_RATE_LIMIT,
    AuthError,
)

REQUIRED = {"angle", "hook", "titles", "visual_direction", "chapters"}


def _response(status=200, content="OK", reasoning=""):
    body = {"choices": [{"message": {"content": content}}]}
    if reasoning:
        body["choices"][0]["message"]["reasoning"] = reasoning
    response = mock.Mock()
    response.status_code = status
    response.json.return_value = body
    response.text = json.dumps(body)
    response.raise_for_status.return_value = None
    return response


def _http_error(status, text="erro"):
    request = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
    return httpx.HTTPStatusError(
        f"HTTP {status}", request=request, response=httpx.Response(status, text=text)
    )


class TestExtractJsonPayload:
    def test_plain_object(self):
        assert pipeline.extract_json_payload('{"a": 1}') == {"a": 1}

    def test_plain_array(self):
        assert pipeline.extract_json_payload('[{"a": 1}]') == [{"a": 1}]

    def test_code_fence_is_stripped(self):
        assert pipeline.extract_json_payload('```json\n{"a": 1}\n```') == {"a": 1}

    def test_reasoning_preamble_is_skipped(self):
        """The real failure: free Nemotron models emit a thinking trace first."""
        raw = (
            "Here's a thinking process:\n\n1.  **Analyze User Input:**\n"
            "   The user wants JSON.\n\n"
            '{"angle": "olha so", "titles": ["a", "b"]}\n\nEspero que ajude!'
        )
        assert pipeline.extract_json_payload(raw) == {"angle": "olha so", "titles": ["a", "b"]}

    def test_nested_objects_and_arrays(self):
        assert pipeline.extract_json_payload('prefix {"a": {"b": [1, 2]}, "c": 3} suffix') == {
            "a": {"b": [1, 2]}, "c": 3,
        }

    def test_braces_inside_strings_do_not_end_the_scan(self):
        assert pipeline.extract_json_payload('{"t": "isto } nao fecha"}') == {"t": "isto } nao fecha"}

    def test_escaped_quote_inside_string(self):
        assert pipeline.extract_json_payload(r'{"t": "diz \" e } continua"}') == {
            "t": 'diz " e } continua'
        }

    def test_array_found_after_object_attempt(self):
        assert pipeline.extract_json_payload('texto {"a": 1} e depois [1, 2, 3]') == {"a": 1}

    def test_raises_when_there_is_no_json(self):
        with pytest.raises(ValueError):
            pipeline.extract_json_payload("apenas texto sem qualquer json")

    def test_raises_on_empty_string(self):
        with pytest.raises(ValueError):
            pipeline.extract_json_payload("")

    def test_raises_on_truncated_json(self):
        with pytest.raises(ValueError):
            pipeline.extract_json_payload('{"a": [1, 2')


class TestCallFreeModelJsonMode:
    def test_json_mode_disables_reasoning_and_requests_json(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
        monkeypatch.setenv("OPENROUTER_MODEL", "vendor/model:free")
        with mock.patch("httpx.post", return_value=_response(content='{"a":1}')) as post:
            pipeline.call_free_model("prompt", json_mode=True)
        payload = post.call_args.kwargs["json"]
        # reasoning.effort=none is what actually produces bare JSON here;
        # reasoning.enabled=false is not supported and errors upstream.
        assert payload["reasoning"] == {"effort": "none"}
        assert payload["response_format"] == {"type": "json_object"}

    def test_plain_mode_does_not_send_reasoning_params(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
        monkeypatch.setenv("OPENROUTER_MODEL", "vendor/model:free")
        with mock.patch("httpx.post", return_value=_response()) as post:
            pipeline.call_free_model("prompt")
        payload = post.call_args.kwargs["json"]
        assert "reasoning" not in payload
        assert "response_format" not in payload

    def test_default_model_is_a_free_variant(self):
        assert pipeline.DEFAULT_FREE_MODEL.endswith(":free")

    def test_missing_key_raises_auth_error(self, monkeypatch):
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        with pytest.raises(AuthError) as exc:
            pipeline.call_free_model("x")
        assert exc.value.error_code == AUTH_MISSING_KEY

    def test_paid_model_is_rejected(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
        monkeypatch.setenv("OPENROUTER_MODEL", "openai/gpt-4o")
        with pytest.raises(AuthError) as exc:
            pipeline.call_free_model("x")
        assert exc.value.error_code == AUTH_MODEL_NOT_FREE

    def test_uses_configured_model(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
        monkeypatch.setenv("OPENROUTER_MODEL", "vendor/other:free")
        with mock.patch("httpx.post", return_value=_response()) as post:
            pipeline.call_free_model("x")
        assert post.call_args.kwargs["json"]["model"] == "vendor/other:free"


class TestTransientRetry:
    def test_retries_resource_exhausted_then_succeeds(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
        monkeypatch.setenv("OPENROUTER_MODEL", "vendor/model:free")
        monkeypatch.setattr(pipeline.time, "sleep", lambda _s: None)

        exhausted = mock.Mock()
        exhausted.status_code = 503
        exhausted.text = '{"error":{"message":"Upstream error from Nvidia: ResourceExhausted"}}'
        with mock.patch("httpx.post", side_effect=[exhausted, _response(content="ok")]) as post:
            assert pipeline.call_free_model("x") == "ok"
        assert post.call_count == 2, "a transient upstream error must be retried"

    def test_gives_up_after_attempts_and_reports_rate_limit(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
        monkeypatch.setenv("OPENROUTER_MODEL", "vendor/model:free")
        monkeypatch.setattr(pipeline.time, "sleep", lambda _s: None)

        throttled = mock.Mock()
        throttled.status_code = 429
        throttled.text = "too many requests"
        with mock.patch("httpx.post", return_value=throttled) as post:
            with pytest.raises(AuthError) as exc:
                pipeline.call_free_model("x")
        assert exc.value.error_code == AUTH_RATE_LIMIT
        assert post.call_count == 3

    def test_does_not_retry_a_rejected_key(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
        monkeypatch.setenv("OPENROUTER_MODEL", "vendor/model:free")
        monkeypatch.setattr(pipeline.time, "sleep", lambda _s: None)

        rejected = mock.Mock()
        rejected.status_code = 401
        rejected.text = "unauthorized"
        with mock.patch("httpx.post", return_value=rejected) as post:
            with pytest.raises(AuthError) as exc:
                pipeline.call_free_model("x")
        assert exc.value.error_code == "AUTH_INVALID_KEY"
        assert post.call_count == 1, "a bad key is not transient"

    def test_reasoning_only_response_is_retried_then_reported(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
        monkeypatch.setenv("OPENROUTER_MODEL", "vendor/model:free")
        monkeypatch.setattr(pipeline.time, "sleep", lambda _s: None)

        thinking = _response(status=200, content="", reasoning="consumi tudo a pensar")
        with mock.patch("httpx.post", return_value=thinking):
            with pytest.raises(AuthError) as exc:
                pipeline.call_free_model("x", json_mode=True)
        assert exc.value.error_code == AUTH_RATE_LIMIT

    def test_network_error_is_wrapped(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
        monkeypatch.setenv("OPENROUTER_MODEL", "vendor/model:free")
        monkeypatch.setattr(pipeline.time, "sleep", lambda _s: None)

        with mock.patch("httpx.post", side_effect=httpx.ConnectError("recusado")):
            with pytest.raises(AuthError) as exc:
                pipeline.call_free_model("x")
        assert exc.value.error_code == "AUTH_REQUEST_FAILED"
        assert exc.value.status_code == 502


class TestStrategyContractOffline:
    """The /api/strategy contract, verified without touching the network.

    The fallback must satisfy the same shape as a model answer, so this pins it
    deterministically instead of depending on a live free-tier call.
    """

    def _post_with(self, monkeypatch, side_effect=None, content=""):
        from fastapi.testclient import TestClient

        from backend import app as app_module

        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
        monkeypatch.setenv("OPENROUTER_MODEL", "vendor/model:free")
        # app.py imports call_free_model into its own namespace, so patching the
        # pipeline module attribute would not intercept it.
        with mock.patch.object(app_module, "call_free_model", side_effect=side_effect) as call:
            call.return_value = content
            return TestClient(app_module.app).post("/api/strategy", json={"niche": "dinheiro"})

    def test_fallback_satisfies_the_contract(self, monkeypatch):
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        data = self._post_with(monkeypatch, side_effect=AuthError(
            AUTH_MISSING_KEY, "OPENROUTER_API_KEY não configurada.", 401,
        )).json()
        assert data["source"] == "fallback-local"
        assert REQUIRED.issubset(data)
        assert len(data["titles"]) >= 3
        assert len(data["chapters"]) >= 3
        assert data["fallback_error"], "a silent fallback hides the reason"

    def test_model_answer_is_accepted_and_labelled(self, monkeypatch):
        good = json.dumps({
            "angle": "o dinheiro como mecanismo",
            "hook": "voce trabalha 2000 horas por ano",
            "titles": ["a", "b", "c"],
            "visual_direction": "documental, tons frios",
            "chapters": ["gancho", "escala", "revelacao"],
        })
        data = self._post_with(monkeypatch, content=good).json()
        assert data["source"] == "openrouter-free"
        assert data["angle"] == "o dinheiro como mecanismo"
        assert data["model"].endswith(":free")
        assert "fallback_error" not in data

    def test_model_answer_behind_a_reasoning_preamble_still_works(self, monkeypatch):
        """The real-world case: free models prepend a thinking trace."""
        noisy = (
            "Here's a thinking process:\n\n1.  **Analyze:** the user wants JSON.\n\n"
            + json.dumps({
                "angle": "olha so", "hook": "gancho", "titles": ["a", "b", "c"],
                "visual_direction": "cinza", "chapters": ["1", "2", "3"],
            })
            + "\n\nEspero ter ajudado!"
        )
        data = self._post_with(monkeypatch, content=noisy).json()
        assert data["source"] == "openrouter-free"
        assert data["angle"] == "olha so"

    def test_short_title_list_falls_back_with_a_reason(self, monkeypatch):
        thin = json.dumps({
            "angle": "a", "hook": "b", "titles": ["so um"],
            "visual_direction": "c", "chapters": ["1", "2", "3"],
        })
        data = self._post_with(monkeypatch, content=thin).json()
        assert data["source"] == "fallback-local"
        assert "titles" in data["fallback_error"]

    def test_non_json_model_output_falls_back(self, monkeypatch):
        data = self._post_with(monkeypatch, content="desculpe, não posso").json()
        assert data["source"] == "fallback-local"

    def test_a_json_array_where_an_object_is_needed_falls_back(self, monkeypatch):
        data = self._post_with(monkeypatch, content=json.dumps([{"angle": "a"}])).json()
        assert data["source"] == "fallback-local"

    def test_missing_chapters_falls_back(self, monkeypatch):
        partial = json.dumps({"angle": "a", "hook": "b", "titles": ["1", "2", "3"]})
        data = self._post_with(monkeypatch, content=partial).json()
        assert data["source"] == "fallback-local"
        assert "chapters" in data["fallback_error"]


class TestDailyQuota:
    """The free tier allows a small number of requests per key per day.

    Hitting it must not look like a transient outage, and the remaining count has to
    reach the UI before the user hits it.
    """

    @staticmethod
    def _quota_response(status=429, remaining="0", limit="50"):
        response = mock.Mock()
        response.status_code = status
        response.text = '{"error":{"message":"Rate limit exceeded: free-models-per-day"}}'
        response.headers = {
            "x-ratelimit-remaining": remaining,
            "x-ratelimit-limit": limit,
        }
        return response

    def test_quota_is_read_from_response_headers(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
        monkeypatch.setenv("OPENROUTER_MODEL", "vendor/model:free")
        monkeypatch.setitem(pipeline._OPENROUTER_QUOTA, "remaining", None)
        monkeypatch.setitem(pipeline._OPENROUTER_QUOTA, "limit", None)

        ok = _response(content="ok")
        ok.headers = {"x-ratelimit-remaining": "37", "x-ratelimit-limit": "50"}
        with mock.patch("httpx.post", return_value=ok):
            pipeline.call_free_model("x")

        status = pipeline.get_provider_status()["openrouter"]
        assert status["quota_remaining"] == 37
        assert status["quota_limit"] == 50
        assert status["quota_exhausted"] is False

    def test_daily_quota_exhaustion_is_not_retried(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
        monkeypatch.setenv("OPENROUTER_MODEL", "vendor/model:free")
        monkeypatch.setattr(pipeline.time, "sleep", lambda _s: None)
        monkeypatch.setitem(pipeline._OPENROUTER_QUOTA, "limit", 50)

        with mock.patch("httpx.post", return_value=self._quota_response()) as post:
            with pytest.raises(AuthError) as exc:
                pipeline.call_free_model("x")
        assert exc.value.error_code == AUTH_QUOTA_EXCEEDED
        assert exc.value.details["scope"] == "daily"
        assert post.call_count == 1, "an exhausted daily quota must not be retried"

    def test_message_tells_the_user_what_to_do(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
        monkeypatch.setenv("OPENROUTER_MODEL", "vendor/model:free")
        with mock.patch("httpx.post", return_value=self._quota_response()):
            with pytest.raises(AuthError) as exc:
                pipeline.call_free_model("x")
        message = exc.value.message.lower()
        assert "quota" in message
        assert "amanhã" in message or "hoje" in message

    def test_status_reports_exhaustion_after_the_header_reads_zero(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
        monkeypatch.setenv("OPENROUTER_MODEL", "vendor/model:free")
        with mock.patch("httpx.post", return_value=self._quota_response(remaining="0")):
            with pytest.raises(AuthError):
                pipeline.call_free_model("x")
        assert pipeline.get_provider_status()["openrouter"]["quota_exhausted"] is True

    def test_providers_endpoint_exposes_the_quota(self, monkeypatch):
        from fastapi.testclient import TestClient

        from backend.app import app

        monkeypatch.setitem(pipeline._OPENROUTER_QUOTA, "remaining", 12)
        monkeypatch.setitem(pipeline._OPENROUTER_QUOTA, "limit", 50)
        openrouter = TestClient(app).get("/api/providers").json()["providers"]["openrouter"]
        assert openrouter["quota_remaining"] == 12
        assert openrouter["quota_limit"] == 50
        assert openrouter["quota_exhausted"] is False


class TestProviderStatus:
    def test_reports_the_configured_model(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_MODEL", "vendor/thing:free")
        status = pipeline.get_provider_status()["openrouter"]
        assert status["model"] == "vendor/thing:free"
        assert status["free_only"] is True

    def test_flags_a_paid_model(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_MODEL", "openai/gpt-4o")
        assert pipeline.get_provider_status()["openrouter"]["free_only"] is False

    def test_media_providers_reflect_their_keys(self, monkeypatch):
        monkeypatch.setenv("PEXELS_API_KEY", "x")
        monkeypatch.delenv("PIXABAY_API_KEY", raising=False)
        status = pipeline.get_provider_status()
        assert status["pexels"]["enabled"] is True
        assert status["pixabay"]["enabled"] is False