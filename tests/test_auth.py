import json
import os
from unittest import mock

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.app import app
from backend.services import pipeline
from backend.services.pipeline import call_free_model
from backend.services.auth_contract import (
    AuthError,
    AUTH_MISSING_KEY,
    AUTH_INVALID_KEY,
    AUTH_RATE_LIMIT,
    AUTH_MODEL_NOT_FREE,
    AUTH_REQUEST_FAILED,
    missing_key_error,
    invalid_key_error,
    rate_limit_error,
    model_not_free_error,
    request_failed_error,
    handle_auth_error,
)

client = TestClient(app)

REQUIRED_STRATEGY_FIELDS = {"angle", "hook", "titles", "visual_direction", "chapters"}


def _http_status_error(status_code: int, text: str = "error") -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
    response = httpx.Response(status_code, request=request, text=text)
    return httpx.HTTPStatusError(f"HTTP {status_code}", request=request, response=response)


def _mock_openrouter_response(content: str = '{"choices":[{"message":{"content":"OK"}}]}') -> mock.Mock:
    response = mock.Mock()
    response.status_code = 200
    response.text = content
    response.json.return_value = json.loads(content)
    response.raise_for_status.return_value = None
    return response


def _payload_with_fake_key(**overrides) -> dict:
    payload = {
        "openrouter_api_key": "sk-or-test-key",
        "openrouter_model": "meta-llama/llama-3.3-8b-instruct:free",
        "pexels_api_key": "",
        "pixabay_api_key": "",
        "gemini_api_key": "",
        "openai_api_key": "",
        "youtube_api_key": "",
    }
    payload.update(overrides)
    return payload


@pytest.fixture(autouse=True)
def _clean_openrouter_env(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_MODEL", "meta-llama/llama-3.3-8b-instruct:free")


class TestMissingApiKey:
    def test_call_free_model_raises_runtime_error(self):
        with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
            call_free_model("test prompt")

    def test_call_free_model_raises_on_empty_string_key(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "")
        with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
            call_free_model("test prompt")

    def test_call_free_model_raises_on_whitespace_key(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "   ")
        with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
            call_free_model("test prompt")

    def test_call_free_model_does_not_call_api_without_key(self):
        with mock.patch("httpx.post") as mock_post:
            with pytest.raises(RuntimeError):
                call_free_model("test prompt")
        mock_post.assert_not_called()

    def test_strategy_returns_fallback_not_500(self):
        response = client.post("/api/strategy", json={"niche": "fitness"})
        assert response.status_code == 200
        data = response.json()
        assert data["source"] == "fallback-local"
        assert "OPENROUTER_API_KEY" in data["fallback_error"]

    def test_strategy_fallback_contains_all_required_fields(self):
        response = client.post("/api/strategy", json={"niche": "fitness"})
        data = response.json()
        assert REQUIRED_STRATEGY_FIELDS.issubset(set(data.keys()))
        assert len(data["titles"]) >= 3
        assert len(data["chapters"]) >= 3

    def test_ai_test_returns_error_body_without_api_key(self):
        response = client.post("/api/ai/test")
        # Real status plus the AUTH_* contract, so the frontend handler can route
        # a missing key to the settings panel instead of seeing a 200.
        assert response.status_code == 401
        body = response.json()
        assert body["status"] == "error"
        assert body["error_code"] == AUTH_MISSING_KEY
        assert "OPENROUTER_API_KEY" in body["message"]
        assert body["details"]["provider"] == "openrouter"


class TestInvalidApiKey:
    def test_call_free_model_handles_401(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-invalid")
        error = _http_status_error(401, '{"error":"invalid api key"}')
        response = mock.Mock()
        response.status_code = 401
        response.text = '{"error":"invalid api key"}'
        response.raise_for_status.side_effect = error
        with mock.patch("httpx.post", return_value=response):
            with pytest.raises(RuntimeError, match="inválida ou não autorizada"):
                call_free_model("test prompt")

    def test_call_free_model_handles_402_no_credits(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
        response = mock.Mock()
        response.status_code = 402
        response.text = "payment required"
        response.raise_for_status.side_effect = _http_status_error(402, "payment required")
        with mock.patch("httpx.post", return_value=response):
            with pytest.raises(RuntimeError, match="Créditos insuficientes"):
                call_free_model("test prompt")

    def test_call_free_model_handles_429_rate_limit(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
        response = mock.Mock()
        response.status_code = 429
        response.text = "too many requests"
        response.raise_for_status.side_effect = _http_status_error(429, "too many requests")
        with mock.patch("httpx.post", return_value=response):
            with pytest.raises(RuntimeError, match="Rate limit"):
                call_free_model("test prompt")

    def test_call_free_model_handles_generic_http_error(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
        response = mock.Mock()
        response.status_code = 500
        response.text = "server exploded"
        response.raise_for_status.side_effect = _http_status_error(500, "server exploded")
        with mock.patch("httpx.post", return_value=response):
            with pytest.raises(RuntimeError, match="HTTP 500"):
                call_free_model("test prompt")

    def test_call_free_model_sends_authorization_header(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-real-key")
        response = _mock_openrouter_response('{"choices":[{"message":{"content":"OK"}}]}')
        with mock.patch("httpx.post", return_value=response) as mock_post:
            result = call_free_model("hello")
        assert result == "OK"
        _, kwargs = mock_post.call_args
        assert kwargs["headers"]["Authorization"] == "Bearer sk-or-real-key"

    def test_ai_test_returns_error_body_on_invalid_key(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-invalid")
        response = mock.Mock()
        response.status_code = 401
        response.text = "unauthorized"
        response.raise_for_status.side_effect = _http_status_error(401, "unauthorized")
        with mock.patch("httpx.post", return_value=response):
            api_response = client.post("/api/ai/test")
        # The endpoint reports the provider's own status and the shared AUTH_*
        # contract. Returning 200 here would hide a rejected key from the frontend
        # auth handler, which routes on 401/403.
        assert api_response.status_code == 401
        body = api_response.json()
        assert body["status"] == "error"
        assert body["error_code"] == AUTH_INVALID_KEY
        assert "OPENROUTER_API_KEY" in body["message"]
        assert body["details"]["provider"] == "openrouter"

    def test_strategy_falls_back_on_invalid_key(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-invalid")
        response = mock.Mock()
        response.status_code = 401
        response.text = "unauthorized"
        response.raise_for_status.side_effect = _http_status_error(401, "unauthorized")
        with mock.patch("httpx.post", return_value=response):
            api_response = client.post("/api/strategy", json={"niche": "fitness"})
        assert api_response.status_code == 200
        data = api_response.json()
        assert data["source"] == "fallback-local"
        assert "OPENROUTER_API_KEY" in data["fallback_error"]
        assert REQUIRED_STRATEGY_FIELDS.issubset(set(data.keys()))

    def test_ai_test_success_with_valid_key(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-valid")
        content = '{"choices":[{"message":{"content":"OK"}}]}'
        response = _mock_openrouter_response(content)
        with mock.patch("httpx.post", return_value=response):
            api_response = client.post("/api/ai/test")
        assert api_response.status_code == 200
        body = api_response.json()
        assert body["status"] == "ok"
        assert body["provider"] == "openrouter"
        assert body["model"].endswith(":free")
        assert body["response"] == "OK"


class TestModelValidation:
    @pytest.mark.parametrize("bad_model", [
        "openai/gpt-4o",
        "meta-llama/llama-3.3-8b-instruct",
        "anthropic/claude-3.5-sonnet",
        "FREE",
        "",
    ])
    def test_call_free_model_rejects_non_free_models(self, monkeypatch, bad_model):
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
        monkeypatch.setenv("OPENROUTER_MODEL", bad_model)
        with mock.patch("httpx.post") as mock_post:
            with pytest.raises(RuntimeError, match=":free"):
                call_free_model("test prompt")
        mock_post.assert_not_called()

    @pytest.mark.parametrize("good_model", [
        "meta-llama/llama-3.3-8b-instruct:free",
        "google/gemma-2-9b-it:free",
        "mistralai/mistral-7b-instruct:free",
    ])
    def test_call_free_model_accepts_free_models(self, monkeypatch, good_model):
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
        monkeypatch.setenv("OPENROUTER_MODEL", good_model)
        response = _mock_openrouter_response('{"choices":[{"message":{"content":"OK"}}]}')
        with mock.patch("httpx.post", return_value=response) as mock_post:
            result = call_free_model("test prompt")
        assert result == "OK"
        _, kwargs = mock_post.call_args
        assert kwargs["json"]["model"] == good_model

    def test_settings_rejects_non_free_model(self):
        response = client.post(
            "/api/settings",
            json=_payload_with_fake_key(openrouter_model="openai/gpt-4o"),
        )
        assert response.status_code == 400
        assert ":free" in response.json()["detail"]

    def test_settings_rejects_non_free_model_without_writing_env(self):
        with mock.patch("pathlib.Path.write_text") as mock_write:
            response = client.post(
                "/api/settings",
                json=_payload_with_fake_key(openrouter_model="meta-llama/llama-3.3-8b-instruct"),
            )
        assert response.status_code == 400
        mock_write.assert_not_called()

    def test_settings_accepts_free_model(self):
        with mock.patch("pathlib.Path.write_text") as mock_write:
            with mock.patch.dict(os.environ, {}, clear=False):
                response = client.post(
                    "/api/settings",
                    json=_payload_with_fake_key(openrouter_model="google/gemma-2-9b-it:free"),
                )
        assert response.status_code == 200
        assert response.json()["status"] == "saved"
        assert mock_write.call_count == 1

    def test_settings_defaults_model_to_free_variant(self):
        with mock.patch("pathlib.Path.write_text") as mock_write:
            with mock.patch.dict(os.environ, {}, clear=False):
                payload = _payload_with_fake_key()
                payload["openrouter_model"] = ""
                response = client.post("/api/settings", json=payload)
        assert response.status_code == 200
        written = mock_write.call_args[0][0]
        # The default must be a :free model that actually has endpoints. Assert the
        # contract, not a specific id: meta-llama/llama-3.3-8b-instruct:free answered
        # 404 "No endpoints found" for real keys.
        model_line = next(line for line in written.splitlines() if line.startswith("OPENROUTER_MODEL="))
        default_model = model_line.split("=", 1)[1]
        assert default_model.endswith(":free")
        assert default_model == pipeline.DEFAULT_FREE_MODEL


class TestStrategyFallback:
    def _post_strategy(self):
        return client.post("/api/strategy", json={"niche": "produtividade"})

    def test_fallback_when_ai_returns_non_json(self):
        with mock.patch("backend.app.call_free_model", return_value="isso não é json"):
            response = self._post_strategy()
        assert response.status_code == 200
        data = response.json()
        assert data["source"] == "fallback-local"
        assert REQUIRED_STRATEGY_FIELDS.issubset(set(data.keys()))
        assert len(data["titles"]) >= 3
        assert len(data["chapters"]) >= 3
        assert "produtividade" in data["angle"]
        assert "produtividade" in data["hook"]

    def test_fallback_when_ai_returns_incomplete_json(self):
        partial = json.dumps({"angle": "some angle", "hook": "some hook"})
        with mock.patch("backend.app.call_free_model", return_value=partial):
            response = self._post_strategy()
        data = response.json()
        assert data["source"] == "fallback-local"
        assert REQUIRED_STRATEGY_FIELDS.issubset(set(data.keys()))

    def test_fallback_when_titles_has_fewer_than_3_items(self):
        payload = {
            "angle": "a",
            "hook": "h",
            "titles": ["only", "two"],
            "visual_direction": "v",
            "chapters": ["c1", "c2", "c3"],
        }
        with mock.patch("backend.app.call_free_model", return_value=json.dumps(payload)):
            response = self._post_strategy()
        data = response.json()
        assert data["source"] == "fallback-local"
        assert len(data["titles"]) >= 3

    def test_fallback_when_chapters_has_fewer_than_3_items(self):
        payload = {
            "angle": "a",
            "hook": "h",
            "titles": ["t1", "t2", "t3"],
            "visual_direction": "v",
            "chapters": ["only", "two"],
        }
        with mock.patch("backend.app.call_free_model", return_value=json.dumps(payload)):
            response = self._post_strategy()
        data = response.json()
        assert data["source"] == "fallback-local"
        assert len(data["chapters"]) >= 3

    def test_fallback_when_ai_raises_exception(self):
        with mock.patch(
            "backend.app.call_free_model",
            side_effect=RuntimeError("OPENROUTER_API_KEY não configurada."),
        ):
            response = self._post_strategy()
        assert response.status_code == 200
        data = response.json()
        assert data["source"] == "fallback-local"
        assert "OPENROUTER_API_KEY" in data["fallback_error"]

    def test_success_persists_valid_ai_response(self):
        payload = {
            "angle": "O custo invisível da procrastinação",
            "hook": "Você perde 2 horas por dia sem perceber",
            "titles": ["Título A", "Título B", "Título C", "Título D"],
            "visual_direction": "Dark, cinematic, mapas animados",
            "chapters": ["Gancho", "Problema", "Mecanismo", "Revelação", "Conclusão"],
        }
        with mock.patch("backend.app.call_free_model", return_value=json.dumps(payload)):
            response = self._post_strategy()
        assert response.status_code == 200
        data = response.json()
        assert data["source"] == "openrouter-free"
        assert data["angle"] == payload["angle"]
        assert data["hook"] == payload["hook"]
        assert data["titles"] == payload["titles"]
        assert data["chapters"] == payload["chapters"]
        assert data["visual_direction"] == payload["visual_direction"]

    def test_success_strips_code_fences_from_ai_response(self):
        payload = {
            "angle": "a",
            "hook": "h",
            "titles": ["t1", "t2", "t3"],
            "visual_direction": "v",
            "chapters": ["c1", "c2", "c3"],
        }
        fenced = "```json\n" + json.dumps(payload) + "\n```"
        with mock.patch("backend.app.call_free_model", return_value=fenced):
            response = self._post_strategy()
        data = response.json()
        assert data["source"] == "openrouter-free"
        assert data["angle"] == "a"

    def test_strategy_requires_niche(self):
        response = client.post("/api/strategy", json={"niche": "   "})
        assert response.status_code == 400

    def test_fallback_fields_are_strings(self):
        with mock.patch("backend.app.call_free_model", return_value="garbage"):
            data = self._post_strategy().json()
        assert isinstance(data["angle"], str) and data["angle"]
        assert isinstance(data["hook"], str) and data["hook"]
        assert isinstance(data["visual_direction"], str) and data["visual_direction"]
        assert all(isinstance(t, str) and t for t in data["titles"])
        assert all(isinstance(c, str) and c for c in data["chapters"])


class TestAuthContract:
    def test_auth_error_attributes(self):
        err = AuthError(
            error_code=AUTH_MISSING_KEY,
            message="No API key configured for provider 'openrouter'.",
            status_code=401,
            details={"provider": "openrouter"},
        )
        assert err.error_code == AUTH_MISSING_KEY
        assert err.message == "No API key configured for provider 'openrouter'."
        assert err.status_code == 401
        assert err.details == {"provider": "openrouter"}
        assert str(err) == "No API key configured for provider 'openrouter'."

    def test_auth_error_details_default_to_empty_dict(self):
        err = AuthError(error_code=AUTH_INVALID_KEY, message="msg", status_code=403)
        assert err.details == {}

    def test_error_code_constants(self):
        assert AUTH_MISSING_KEY == "AUTH_MISSING_KEY"
        assert AUTH_INVALID_KEY == "AUTH_INVALID_KEY"
        assert AUTH_RATE_LIMIT == "AUTH_RATE_LIMIT"
        assert AUTH_MODEL_NOT_FREE == "AUTH_MODEL_NOT_FREE"
        assert AUTH_REQUEST_FAILED == "AUTH_REQUEST_FAILED"

    def test_missing_key_error(self):
        err = missing_key_error("openrouter")
        assert isinstance(err, AuthError)
        assert err.error_code == AUTH_MISSING_KEY
        assert err.status_code == 401
        assert err.details == {"provider": "openrouter"}
        assert "openrouter" in err.message

    def test_invalid_key_error(self):
        err = invalid_key_error("openrouter")
        assert isinstance(err, AuthError)
        assert err.error_code == AUTH_INVALID_KEY
        assert err.status_code == 403
        assert err.details["provider"] == "openrouter"

    def test_invalid_key_error_merges_details(self):
        err = invalid_key_error("openrouter", details={"http_status": 401})
        assert err.details["provider"] == "openrouter"
        assert err.details["http_status"] == 401

    def test_rate_limit_error(self):
        err = rate_limit_error("openrouter")
        assert isinstance(err, AuthError)
        assert err.error_code == AUTH_RATE_LIMIT
        assert err.status_code == 429
        assert err.details == {"provider": "openrouter"}

    def test_model_not_free_error(self):
        err = model_not_free_error("openai/gpt-4o")
        assert isinstance(err, AuthError)
        assert err.error_code == AUTH_MODEL_NOT_FREE
        assert err.status_code == 400
        assert err.details == {"model": "openai/gpt-4o"}
        assert "openai/gpt-4o" in err.message

    def test_request_failed_error(self):
        original = ValueError("connection reset")
        err = request_failed_error("openrouter", original)
        assert isinstance(err, AuthError)
        assert err.error_code == AUTH_REQUEST_FAILED
        assert err.status_code == 502
        assert err.details["provider"] == "openrouter"
        assert err.details["exception"] == "connection reset"

    def test_handle_auth_error_structure(self):
        err = missing_key_error("openrouter")
        result = handle_auth_error(err)
        assert result["status_code"] == 401
        assert result["body"]["error_code"] == AUTH_MISSING_KEY
        assert result["body"]["message"] == err.message
        assert result["body"]["details"] == {"provider": "openrouter"}

    def test_handle_auth_error_covers_all_codes(self):
        providers = ["openrouter", "pexels", "gemini"]
        for provider in providers:
            for helper in (missing_key_error, invalid_key_error, rate_limit_error):
                result = handle_auth_error(helper(provider))
                assert result["status_code"] == helper(provider).status_code
                assert result["body"]["details"]["provider"] == provider

    async def test_auth_contract_usable_in_async_context(self):
        err = missing_key_error("openrouter")
        result = handle_auth_error(err)
        assert result["status_code"] == 401
        assert result["body"]["error_code"] == AUTH_MISSING_KEY
