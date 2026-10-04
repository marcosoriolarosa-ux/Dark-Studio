import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
import requests

BASE_DIR = Path(__file__).resolve().parents[1]
SERVER_HOST = "127.0.0.1"
SERVER_PORT = 8765
BASE_URL = f"http://{SERVER_HOST}:{SERVER_PORT}"

REQUIRED_STRATEGY_FIELDS = {"angle", "hook", "titles", "visual_direction", "chapters"}

# /api/build-video runs a real HyperFrames render: npx + chrome-headless-shell +
# ffmpeg. One 41s 1080x1920 cut was measured at ~316s on a 2-vCPU box, and CI or
# a laptop under load is slower still. The old 120s budget was the TEST being
# wrong, not the endpoint: the render returns 200 given long enough. Do not
# tighten this without timing a real render on a slow machine first - the fix for
# an unbounded render is the server's watchdog, not a shorter client timeout.
BUILD_VIDEO_TIMEOUT = 900


def _wait_for_server(timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((SERVER_HOST, SERVER_PORT), timeout=1):
                return True
        except OSError:
            time.sleep(0.3)
    return False


@pytest.fixture(scope="session")
def server():
    env = os.environ.copy()
    env["PYTHONPATH"] = str(BASE_DIR)
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "backend.app:app", "--host", SERVER_HOST, "--port", str(SERVER_PORT)],
        cwd=str(BASE_DIR),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if not _wait_for_server():
        proc.terminate()
        raise RuntimeError("Server did not start in time")
    yield BASE_URL
    proc.terminate()
    proc.wait(timeout=10)


@pytest.fixture(scope="session")
def base_url(server):
    return server


class TestServerHealth:
    def test_health_returns_200(self, base_url):
        resp = requests.get(f"{base_url}/health", timeout=10)
        assert resp.status_code == 200

    def test_health_returns_valid_json(self, base_url):
        resp = requests.get(f"{base_url}/health", timeout=10)
        data = resp.json()
        assert data["status"] == "ok"
        assert "message" in data


class TestProvidersEndpoint:
    def test_providers_return_200(self, base_url):
        resp = requests.get(f"{base_url}/api/providers", timeout=10)
        assert resp.status_code == 200

    def test_providers_structure(self, base_url):
        resp = requests.get(f"{base_url}/api/providers", timeout=10)
        data = resp.json()
        assert "providers" in data
        providers = data["providers"]
        expected = {"gemini", "openai", "youtube", "pexels", "pixabay", "openrouter"}
        assert expected.issubset(set(providers.keys()))
        for name, info in providers.items():
            assert isinstance(info, dict)
            assert "enabled" in info


@pytest.fixture(scope="session")
def strategy_payload(server):
    """One live /api/strategy response shared by every contract assertion.

    Each request costs a real free-tier model call (13-60s, and the tier
    intermittently stalls), so asserting the contract over six separate calls made
    the suite slow and flaky. The shape is the same whichever source answered.
    """
    resp = requests.post(f"{server}/api/strategy", json={"niche": "fitness"}, timeout=120)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert isinstance(data, dict)
    return data


class TestStrategyEndpoint:
    """Contract tests: valid whether the free model answered or the fallback did."""

    def test_strategy_returns_200(self, base_url):
        resp = requests.post(f"{base_url}/api/strategy", json={"niche": "fitness"}, timeout=120)
        assert resp.status_code == 200

    def test_strategy_rejects_an_empty_niche(self, base_url):
        resp = requests.post(f"{base_url}/api/strategy", json={"niche": "   "}, timeout=30)
        assert resp.status_code == 400

    def test_strategy_source_is_declared(self, strategy_payload):
        assert strategy_payload.get("source") in ("fallback-local", "openrouter-free")

    def test_strategy_always_carries_the_required_fields(self, strategy_payload):
        assert REQUIRED_STRATEGY_FIELDS.issubset(set(strategy_payload.keys()))

    def test_strategy_field_types_are_sane(self, strategy_payload):
        data = strategy_payload
        assert isinstance(data["angle"], str) and data["angle"].strip()
        assert isinstance(data["hook"], str) and data["hook"].strip()
        assert isinstance(data["visual_direction"], str) and data["visual_direction"].strip()
        assert isinstance(data["titles"], list) and data["titles"]
        assert isinstance(data["chapters"], list) and data["chapters"]
        assert all(isinstance(item, str) and item.strip() for item in data["titles"])
        assert all(isinstance(item, str) and item.strip() for item in data["chapters"])

    def test_fallback_never_masquerades_as_a_model_answer(self, strategy_payload):
        data = strategy_payload
        if data["source"] == "fallback-local":
            assert data["fallback_error"], "a silent fallback hides the reason"
            assert len(data["titles"]) >= 3
            assert len(data["chapters"]) >= 3


class TestAuthContract:
    def test_auth_error_class_importable(self):
        from backend.services.auth_contract import AuthError
        err = AuthError(error_code="TEST", message="msg", status_code=400)
        assert err.error_code == "TEST"
        assert err.message == "msg"
        assert err.status_code == 400
        assert str(err) == "msg"

    def test_auth_error_details_default(self):
        from backend.services.auth_contract import AuthError
        err = AuthError(error_code="TEST", message="msg", status_code=400)
        assert err.details == {}

    def test_missing_key_error(self):
        from backend.services.auth_contract import missing_key_error, AUTH_MISSING_KEY
        err = missing_key_error("openrouter")
        assert err.error_code == AUTH_MISSING_KEY
        assert err.status_code == 401
        assert err.details["provider"] == "openrouter"

    def test_invalid_key_error(self):
        from backend.services.auth_contract import invalid_key_error, AUTH_INVALID_KEY
        err = invalid_key_error("pexels")
        assert err.error_code == AUTH_INVALID_KEY
        assert err.status_code == 403
        assert err.details["provider"] == "pexels"

    def test_rate_limit_error(self):
        from backend.services.auth_contract import rate_limit_error, AUTH_RATE_LIMIT
        err = rate_limit_error("gemini")
        assert err.error_code == AUTH_RATE_LIMIT
        assert err.status_code == 429

    def test_model_not_free_error(self):
        from backend.services.auth_contract import model_not_free_error, AUTH_MODEL_NOT_FREE
        err = model_not_free_error("openai/gpt-4o")
        assert err.error_code == AUTH_MODEL_NOT_FREE
        assert err.status_code == 400
        assert err.details["model"] == "openai/gpt-4o"

    def test_request_failed_error(self):
        from backend.services.auth_contract import request_failed_error, AUTH_REQUEST_FAILED
        err = request_failed_error("openrouter", ValueError("conn reset"))
        assert err.error_code == AUTH_REQUEST_FAILED
        assert err.status_code == 502
        assert err.details["exception"] == "conn reset"

    def test_handle_auth_error_structure(self):
        from backend.services.auth_contract import handle_auth_error, missing_key_error
        result = handle_auth_error(missing_key_error("openrouter"))
        assert result["status_code"] == 401
        assert result["body"]["error_code"] == "AUTH_MISSING_KEY"
        assert "provider" in result["body"]["details"]


class TestFrontendServing:
    def test_app_returns_html(self, base_url):
        resp = requests.get(f"{base_url}/app/", timeout=10)
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("Content-Type", "")

    def test_app_contains_html_tags(self, base_url):
        resp = requests.get(f"{base_url}/app/", timeout=10)
        text = resp.text
        assert "<html" in text.lower()
        assert "<body" in text.lower() or "<div" in text.lower()


@pytest.mark.slow
class TestBuildVideo:
    def test_build_video_returns_200(self, base_url):
        resp = requests.post(
            f"{base_url}/api/build-video",
            data={"project_name": "api_test3"},
            timeout=BUILD_VIDEO_TIMEOUT,
        )
        assert resp.status_code == 200, resp.text

    def test_build_video_response_structure(self, base_url):
        resp = requests.post(
            f"{base_url}/api/build-video",
            data={"project_name": "api_test3"},
            timeout=BUILD_VIDEO_TIMEOUT,
        )
        # Status first: a non-JSON error body must fail here as a readable
        # mismatch, not one line later as a confusing JSONDecodeError.
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["project_name"] == "api_test3"
        assert "storyboard" in data
        assert "render_real" in data
        assert isinstance(data["storyboard"], list)
        assert len(data["storyboard"]) > 0
