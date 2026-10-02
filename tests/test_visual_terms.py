"""Tests for visual search-term extraction.

Picking stock media from caption words fails in a specific way: "explanao
superficial" made Pexels return a macro photograph of skin, because the query is
abstract Portuguese. Asking the model for concrete English visual terms fixes both
problems, but the model is optional (no key, or the daily quota is spent), so the
heuristic path has to stand on its own.
"""
import json
from unittest import mock

import pytest

from backend.services import pipeline


CAPTIONS = [
    {"index": 1, "caption": "Voce trabalha cerca de dois mil horas por ano para acumular papeis coloridos"},
    {"index": 2, "caption": "A explicacao superficial diz que o dinheiro foi inventado para facilitar a troca"},
    {"index": 3, "caption": "Entender como essa engrenagem funciona e a unica diferenca entre quem enriquece"},
]


class TestKeywordExtraction:
    def test_srt_timestamps_never_become_search_terms(self):
        srt = "1\n00:00:00,000 --> 00:00:06,500\nO dinheiro moderno\n\n2\n00:00:06,500 --> 00:00:12,500\n"
        keywords = pipeline.extract_keywords_from_text(srt)
        assert not any(":" in k for k in keywords), f"timestamps leaked: {keywords}"
        assert not any(k.isdigit() for k in keywords)

    def test_stopwords_and_verbs_are_dropped(self):
        keywords = pipeline.extract_keywords_from_text("Isso e o que te ensinaram na escola para continuar")
        assert "isso" not in keywords
        assert "ensinaram" not in keywords
        assert "continuar" not in keywords

    def test_concrete_subjects_survive(self):
        keywords = pipeline.extract_keywords_from_text("Voce acumula papeis coloridos em notas de dinheiro")
        assert "papeis" in keywords or "coloridos" in keywords
        assert "dinheiro" in keywords

    def test_phrases_are_offered(self):
        keywords = pipeline.extract_keywords_from_text("notas de dinheiroUE", limit=4)
        assert any(" " in k for k in keywords), "adjacent content words should form a phrase"

    def test_empty_input(self):
        assert pipeline.extract_keywords_from_text("") == []
        assert pipeline.extract_keywords_from_text("   ") == []

    def test_respects_the_limit(self):
        text = "papeis coloridos dinheiro mercado bancos engrenagem riqueza pessoas tempo"
        assert len(pipeline.extract_keywords_from_text(text, limit=3)) <= 3

    def test_no_phrase_invents_a_word_the_text_did_not_contain(self):
        text = "papeis coloridos e notas de dinheiro"
        keywords = pipeline.extract_keywords_from_text(text)
        vocabulary = set(text.lower().replace(" e ", " ").split())
        for keyword in keywords:
            for part in keyword.split():
                assert part in vocabulary, f"phrase invented the word {part!r}"

    def test_scene_keywords_differ_between_scenes(self):
        per_scene = pipeline.extract_scene_keywords(CAPTIONS, limit=2)
        assert len(per_scene) == len(CAPTIONS)
        first, second, third = (per_scene[i] for i in range(3))
        assert first and second and third
        assert first != second != third, "every scene searching the same words defeats the purpose"


class TestAiVisualTerms:
    def _storyboard(self):
        return [{"index": i, "start": i * 3.0, "end": i * 3.0 + 3.0,
                 "caption": CAPTIONS[i % len(CAPTIONS)]["caption"]}
                for i in range(3)]

    def test_returns_none_without_a_key(self, monkeypatch):
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        assert pipeline.extract_visual_terms_with_ai(self._storyboard()) is None

    def test_skips_the_call_when_the_quota_is_spent(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
        monkeypatch.setitem(pipeline._OPENROUTER_QUOTA, "remaining", 0)
        with mock.patch.object(pipeline, "call_free_model") as call:
            assert pipeline.extract_visual_terms_with_ai(self._storyboard()) is None
        assert call.call_count == 0, "a spent quota must not be spent on a doomed call"

    def test_parses_the_model_answer(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
        monkeypatch.setitem(pipeline._OPENROUTER_QUOTA, "remaining", 40)
        payload = json.dumps({"terms": {
            "0": ["banknotes", "currency"],
            "1": ["history of money"],
            "2": ["bank building"],
        }})
        with mock.patch.object(pipeline, "call_free_model", return_value=payload):
            terms = pipeline.extract_visual_terms_with_ai(self._storyboard())
        assert terms[0] == ["banknotes", "currency"]
        assert terms[1] == ["history of money"]

    def test_accepts_a_bare_string_term(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
        monkeypatch.setitem(pipeline._OPENROUTER_QUOTA, "remaining", 40)
        payload = json.dumps({"terms": {"0": "banknotes"}})
        with mock.patch.object(pipeline, "call_free_model", return_value=payload):
            terms = pipeline.extract_visual_terms_with_ai(self._storyboard())
        assert terms[0] == ["banknotes"]

    def test_accepts_a_flat_mapping_without_the_wrapper(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
        monkeypatch.setitem(pipeline._OPENROUTER_QUOTA, "remaining", 40)
        payload = json.dumps({"0": ["banknotes"], "1": ["coins"]})
        with mock.patch.object(pipeline, "call_free_model", return_value=payload):
            terms = pipeline.extract_visual_terms_with_ai(self._storyboard())
        assert terms[0] == ["banknotes"]

    def test_tolerates_a_reasoning_preamble(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
        monkeypatch.setitem(pipeline._OPENROUTER_QUOTA, "remaining", 40)
        payload = "Here's a thinking process:\n\n1. Consider the scenes.\n\n" + json.dumps(
            {"terms": {"0": ["banknotes"]}}
        )
        with mock.patch.object(pipeline, "call_free_model", return_value=payload):
            terms = pipeline.extract_visual_terms_with_ai(self._storyboard())
        assert terms[0] == ["banknotes"]

    def test_provider_failure_returns_none(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
        monkeypatch.setitem(pipeline._OPENROUTER_QUOTA, "remaining", 40)
        with mock.patch.object(pipeline, "call_free_model", side_effect=RuntimeError("404")):
            assert pipeline.extract_visual_terms_with_ai(self._storyboard()) is None

    def test_unusable_output_returns_none(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
        monkeypatch.setitem(pipeline._OPENROUTER_QUOTA, "remaining", 40)
        with mock.patch.object(pipeline, "call_free_model", return_value="not json at all"):
            assert pipeline.extract_visual_terms_with_ai(self._storyboard()) is None

    def test_empty_terms_return_none(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
        monkeypatch.setitem(pipeline._OPENROUTER_QUOTA, "remaining", 40)
        with mock.patch.object(pipeline, "call_free_model", return_value=json.dumps({"terms": {}})):
            assert pipeline.extract_visual_terms_with_ai(self._storyboard()) is None


class TestSearchMediaForScenes:
    def _storyboard(self):
        return [{"index": i, "start": i * 3.0, "end": i * 3.0 + 3.0,
                 "caption": CAPTIONS[i % len(CAPTIONS)]["caption"]}
                for i in range(3)]

    def _enabled(self, monkeypatch):
        monkeypatch.setattr(
            pipeline, "get_provider_status",
            lambda: {"pexels": {"enabled": True}, "pixabay": {"enabled": False},
                     "openrouter": {"enabled": True, "quota_exhausted": False}},
        )

    def test_no_media_provider_returns_nothing(self, monkeypatch):
        monkeypatch.setattr(pipeline, "get_provider_status", lambda: {"pexels": {"enabled": False}})
        per_scene, source = pipeline.search_media_for_scenes(self._storyboard())
        assert per_scene == {}
        assert source == "none"

    def test_ai_terms_win_when_available(self, monkeypatch):
        self._enabled(monkeypatch)
        monkeypatch.setattr(
            pipeline, "extract_visual_terms_with_ai",
            lambda sb, niche="": {0: ["banknotes"], 1: ["coins"], 2: ["bank"]},
        )
        seen = []

        def fake_search(keywords):
            seen.append(keywords[0])
            return [{"url": f"https://x.test/{keywords[0]}.jpg", "source": "Pexels", "keyword": keywords[0]}]

        monkeypatch.setattr(pipeline, "search_media_for_keywords", fake_search)
        per_scene, source = pipeline.search_media_for_scenes(self._storyboard())
        assert source == "ai"
        assert seen[0] == "banknotes", "the model's English terms must be the ones queried"
        assert per_scene[0][0]["keyword"] == "banknotes"

    def test_falls_back_to_the_heuristic_when_there_is_no_model(self, monkeypatch):
        self._enabled(monkeypatch)
        monkeypatch.setattr(pipeline, "extract_visual_terms_with_ai", lambda sb, niche="": None)

        seen = []

        def fake_search(keywords):
            seen.append(keywords[0])
            return [{"url": f"https://x.test/{abs(hash(keywords[0]))}.jpg", "source": "Pexels", "keyword": keywords[0]}]

        monkeypatch.setattr(pipeline, "search_media_for_keywords", fake_search)
        per_scene, source = pipeline.search_media_for_scenes(self._storyboard())
        assert source == "heuristic"
        assert per_scene[0], "the fallback path must still produce media"

    def test_a_scene_the_model_omitted_still_gets_media(self, monkeypatch):
        self._enabled(monkeypatch)
        monkeypatch.setattr(
            pipeline, "extract_visual_terms_with_ai",
            lambda sb, niche="": {0: ["banknotes"]},
        )
        monkeypatch.setattr(
            pipeline, "search_media_for_keywords",
            lambda kw: [{"url": f"https://x.test/{kw[0]}.jpg", "source": "Pexels", "keyword": kw[0]}],
        )
        per_scene, source = pipeline.search_media_for_scenes(self._storyboard())
        assert source == "ai"
        for idx in range(3):
            assert per_scene[idx], f"scene {idx} got no media at all"

    def test_duplicate_urls_within_a_scene_are_dropped(self, monkeypatch):
        self._enabled(monkeypatch)
        monkeypatch.setattr(pipeline, "extract_visual_terms_with_ai", lambda sb, niche="": None)
        monkeypatch.setattr(
            pipeline, "search_media_for_keywords",
            lambda kw: [
                {"url": "https://x.test/same.jpg", "source": "Pexels", "keyword": kw[0]},
                {"url": "https://x.test/same.jpg", "source": "Pixabay", "keyword": kw[0]},
            ],
        )
        per_scene, _source = pipeline.search_media_for_scenes(self._storyboard())
        urls = [item["url"] for item in per_scene[0]]
        assert len(urls) == len(set(urls))

    def test_a_scene_with_no_usable_terms_gets_no_media(self, monkeypatch):
        self._enabled(monkeypatch)
        monkeypatch.setattr(pipeline, "extract_visual_terms_with_ai", lambda sb, niche="": None)
        monkeypatch.setattr(pipeline, "extract_scene_keywords", lambda sb, limit=3: {})
        monkeypatch.setattr(pipeline, "search_media_for_keywords", lambda kw: [])
        per_scene, source = pipeline.search_media_for_scenes(self._storyboard())
        assert source == "heuristic"
        assert all(items == [] for items in per_scene.values())


class TestBuildVideoTermReporting:
    def test_response_declares_the_term_source(self, monkeypatch):
        from fastapi.testclient import TestClient

        from backend import app as app_module
        from backend.services.pipeline import UPLOAD_DIR

        srt = UPLOAD_DIR / "termos.srt"
        original = srt.exists()
        if not original:
            srt.write_text(
                "1\n00:00:00,000 --> 00:00:04,000\nVoce acumula papeis coloridos\n",
                encoding="utf-8",
            )
        monkeypatch.setattr(
            app_module, "search_media_for_scenes",
            lambda sb, **kw: ({0: [{"url": "https://x.test/a.jpg", "source": "Pexels", "keyword": "banknotes"}]}, "ai"),
        )
        monkeypatch.setattr(app_module, "search_media_for_keywords", lambda kw: [])
        monkeypatch.setattr(
            app_module, "render_video_hyperframes",
            mock.AsyncMock(return_value={"status": "rendered", "output_path": "x.mp4"}),
        )
        try:
            data = TestClient(app_module.app).post(
                "/api/build-video", data={"project_name": "termos"}
            ).json()
            assert data["term_source"] == "ai"
        finally:
            if not original:
                srt.unlink(missing_ok=True)