"""Opt-in tests that hit the real free providers.

Deselected by default. Run them with:

    pytest -m live

They are separated from the rest of the suite because the free tier is slow
(10-60s per model call), occasionally stalls, and needs real keys. The contract
itself is covered offline in tests/test_ai_gateway.py.
"""
import os
import sys
from pathlib import Path

import pytest
import requests

BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))

from dotenv import load_dotenv

load_dotenv(BASE_DIR / ".env", override=True)

pytestmark = pytest.mark.live

needs_openrouter = pytest.mark.skipif(
    not os.getenv("OPENROUTER_API_KEY"), reason="OPENROUTER_API_KEY not configured"
)
needs_media = pytest.mark.skipif(
    not (os.getenv("PEXELS_API_KEY") or os.getenv("PIXABAY_API_KEY")),
    reason="no stock media key configured",
)


class TestLiveModel:
    @needs_openrouter
    def test_configured_model_actually_answers(self):
        from backend.services.pipeline import call_free_model

        answer = call_free_model("Responda apenas: OK", max_tokens=16)
        assert answer.strip(), "the configured :free model returned nothing"

    @needs_openrouter
    def test_json_mode_returns_parsable_json(self):
        from backend.services.pipeline import call_free_model, extract_json_payload

        raw = call_free_model(
            'Responda APENAS JSON: {"ok": true, "numeros": [1, 2, 3]}',
            json_mode=True,
            max_tokens=120,
        )
        parsed = extract_json_payload(raw)
        assert isinstance(parsed, dict)
        assert parsed.get("ok") is True

    @needs_openrouter
    def test_default_free_model_has_endpoints(self):
        """Guards against the original bug: a :free model with no endpoints."""
        from backend.services.pipeline import DEFAULT_FREE_MODEL, call_free_model

        saved = os.environ.get("OPENROUTER_MODEL")
        os.environ["OPENROUTER_MODEL"] = DEFAULT_FREE_MODEL
        try:
            assert call_free_model("Responda apenas: OK", max_tokens=16).strip()
        finally:
            if saved is None:
                os.environ.pop("OPENROUTER_MODEL", None)
            else:
                os.environ["OPENROUTER_MODEL"] = saved

    @needs_openrouter
    def test_strategy_uses_the_model_not_the_fallback(self):
        from fastapi.testclient import TestClient

        from backend.app import app

        data = TestClient(app).post(
            "/api/strategy",
            json={"niche": "porque o dinheiro moderno deixou de ser riqueza"},
        ).json()
        assert data["source"] == "openrouter-free", f"fell back: {data.get('fallback_error')}"
        assert len(data["titles"]) >= 3
        assert len(data["chapters"]) >= 3

    @needs_openrouter
    def test_shorts_highlights_come_from_the_model(self):
        from backend.services.pipeline import parse_srt_to_segments
        from backend.services.shorts_pipeline import detect_highlights

        srt = BASE_DIR / "storage" / "uploads" / "api_test3.srt"
        if not srt.exists():
            pytest.skip("fixture srt not present")
        segments = parse_srt_to_segments(srt)
        highlights = detect_highlights(segments, srt.read_text(encoding="utf-8"))
        assert highlights
        bounds = [(s["start"], s["end"]) for s in segments]
        for item in highlights:
            assert any(low <= item["start"] and item["end"] <= high for low, high in bounds), (
                f"highlight {item} escaped its source segment"
            )


class TestLiveStockMedia:
    @needs_media
    def test_pexels_returns_usable_images(self):
        from backend.services.pipeline import fetch_provider_media

        results = fetch_provider_media("cidade à noite", "pexels")
        assert results, "Pexels returned nothing for a common query"
        assert all(item["url"].startswith("http") for item in results)
        assert all(item["kind"] in {"image", "video"} for item in results)

    @needs_media
    def test_pixabay_returns_full_resolution_images(self):
        from backend.services.pipeline import fetch_provider_media

        if not os.getenv("PIXABAY_API_KEY"):
            pytest.skip("no Pixabay key")
        results = fetch_provider_media("cidade à noite", "pixabay")
        assert results
        # webformatURL is the small variant; the pipeline must not settle for it.
        assert any(item["width"] and item["width"] >= 1000 for item in results), (
            "Pixabay returned only low-resolution results"
        )

    @needs_media
    def test_keyword_search_reaches_a_provider(self):
        from backend.services.pipeline import _MEDIA_CACHE, search_media_for_keywords

        _MEDIA_CACHE.clear()
        results = search_media_for_keywords(["economia", "dinheiro"])
        assert results, "keyword search returned nothing with a provider configured"
        assert all(item["keyword"] in {"economia", "dinheiro"} for item in results)

    @needs_media
    def test_a_downloaded_asset_is_a_real_image(self):
        """Guards the defect where assets sat outside the composition directory."""
        import httpx

        from backend.services.pipeline import fetch_provider_media, download_media_asset

        results = fetch_provider_media("oceano", "pexels")
        if not results:
            pytest.skip("no results")
        path = download_media_asset(results[0]["url"], "live_probe", 0)
        assert path is not None and path.exists()
        assert path.stat().st_size > 5000, "downloaded asset is suspiciously small"
        head = httpx.get(results[0]["url"], timeout=25).content[:3]
        assert head in (b"\xff\xd8\xff", b"\x89PN", b"RIFF"), "not a JPEG/PNG/WebP payload"