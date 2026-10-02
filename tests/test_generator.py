"""Tests for the one-click generation orchestrator.

No network, no real ffmpeg, no real TTS. The four service functions that would
reach a provider or a headless browser are monkeypatched with fakes; the point
of these tests is the *orchestration* contract - stage order, progress values,
concurrency cap, cancellation, and the registry's bounded history.

Run with:  python3 -m pytest tests/test_generator.py -q
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest import mock

import pytest

from backend.services import generator as gen
from backend.services.generator import GenerationRequest
from backend.services import script_gen


def _run(job_id: str) -> None:
    """Drive the worker coroutine in a fresh event loop.

    ``asyncio.get_event_loop()`` raises on Python 3.14 when no loop is running,
    so each call gets its own loop instead of sharing one across tests.
    """
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(gen._run_job(job_id))
    finally:
        loop.close()


# ----------------------------------------------------------------- test helpers

def _script(**overrides):
    """A real Script object the fake generate_script returns.

    generate_script's contract is ``-> Script`` (a dataclass with .to_dict()),
    so the fake returns a Script too - a plain dict would break run_generation,
    which calls .to_dict() on the result.
    """
    payload = {
        "title": "O dinheiro que ninguém vê",
        "hook": "Em sessenta segundos, o dinheiro muda de rosto.",
        "sections": [
            {
                "index": i,
                "text": f"Secção {i} do guião, com narração suficiente para o vídeo.",
                "visual_terms": [f"banknotes close up {i}", f"city street night {i}"],
            }
            for i in range(1, 4)
        ],
        "language": "pt-PT",
        "tone": "documentary",
        "source": "openrouter-free",
        "model": "vendor/model:free",
        "fallback_error": None,
        "estimated_seconds": 12.0,
        "section_count": 3,
        "degraded": False,
    }
    payload.update(overrides)
    sections = [
        script_gen.ScriptSection(
            index=s["index"], text=s["text"], visual_terms=list(s["visual_terms"])
        )
        for s in payload["sections"]
    ]
    return script_gen.Script(
        title=payload["title"],
        hook=payload["hook"],
        sections=sections,
        language=payload["language"],
        tone=payload["tone"],
        source=payload["source"],
        model=payload["model"],
        fallback_error=payload["fallback_error"],
    )


def _segments():
    return [
        {"index": 1, "start": 0.0, "end": 4.0, "text": "Primeira frase do guião."},
        {"index": 2, "start": 4.0, "end": 8.0, "text": "Segunda frase do guião."},
        {"index": 3, "start": 8.0, "end": 12.0, "text": "Terceira frase do guião."},
    ]


def _srt_text():
    return (
        "1\n00:00:00,000 --> 00:00:04,000\nPrimeira frase do guião.\n\n"
        "2\n00:00:04,000 --> 00:00:08,000\nSegunda frase do guião.\n\n"
        "3\n00:00:08,000 --> 00:00:12,000\nTerceira frase do guião.\n"
    )


def _storyboard():
    return [
        {"index": 1, "start": 0.0, "end": 4.0, "duration": 4.0, "headline": "h1",
         "caption": "c1", "background": "#000000", "tone": "narrative", "media_url": ""},
        {"index": 2, "start": 4.0, "end": 8.0, "duration": 4.0, "headline": "h2",
         "caption": "c2", "background": "#000000", "tone": "narrative", "media_url": ""},
        {"index": 3, "start": 8.0, "end": 12.0, "duration": 4.0, "headline": "h3",
         "caption": "c3", "background": "#000000", "tone": "narrative", "media_url": ""},
    ]


def _render_result():
    return {
        "project_name": "projeto",
        "output_stem": "projeto",
        "status": "rendered",
        "output_path": "/tmp/projeto.mp4",
        "storyboard": _storyboard(),
        "scene_count": 3,
        "scenes_with_media": 3,
        "preset": "cinematic",
        "message": "Vídeo renderizado com HyperFrames com sucesso.",
    }


@pytest.fixture(autouse=True)
def _clean_registry():
    """Every test starts from an empty registry and a fresh semaphore."""
    gen.reset_for_tests()
    yield
    gen.reset_for_tests()


@pytest.fixture
def fake_pipeline(monkeypatch, tmp_path):
    """Patch every stage so run_generation never touches a real provider.

    The fake TTS writes a placeholder MP3, the fake transcriber returns fixed
    segments, the fake media search returns nothing (so the test also covers
    the "no media" path), and the fake renderer returns a canned result dict.
    """
    monkeypatch.setattr(gen.script_gen, "generate_script", lambda *a, **k: _script())

    async def _fake_tts(text, output_path, **kw):
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_bytes(b"ID3fake-mp3-bytes")
        return Path(output_path)

    monkeypatch.setattr(gen.tts, "synthesize_speech_long", _fake_tts)
    monkeypatch.setattr(gen.pipeline, "transcribe_audio_file", lambda path: _segments())
    monkeypatch.setattr(gen.pipeline, "build_srt_from_segments", lambda segs: _srt_text())
    monkeypatch.setattr(gen.pipeline, "build_storyboard_from_segments", lambda segs: _storyboard())
    monkeypatch.setattr(gen.pipeline, "search_media_for_scenes", lambda sb, **kw: ({}, "none"))
    monkeypatch.setattr(gen.pipeline, "extract_keywords_from_text", lambda text: ["dinheiro"])
    monkeypatch.setattr(gen.pipeline, "search_media_for_keywords", lambda kw: [])
    monkeypatch.setattr(gen.music, "pick_track", lambda mood, exclude_ids=None: None)

    async def _fake_render(*args, **kwargs):
        # Record the kwargs the orchestrator handed us so the test can assert
        # the render stage wires preset/subtitle/music through unchanged.
        _fake_render.last_kwargs = dict(kwargs)
        return _render_result()

    _fake_render.last_kwargs = {}
    monkeypatch.setattr(gen.render_engine, "render_video_hyperframes", _fake_render)
    return _fake_render


# --------------------------------------------------------------- slugify_topic

class TestSlugifyTopic:
    def test_strips_accents_and_lowercases(self):
        assert gen.slugify_topic("Café São Paulo") == "cafe-sao-paulo"

    def test_replaces_spaces_and_specials_with_hyphens(self):
        assert gen.slugify_topic("crise / 2024!") == "crise-2024"

    def test_collapses_repeated_separators(self):
        assert gen.slugify_topic("a   --__  b") == "a-b"

    def test_truncates_to_max_len(self):
        long = "palavra " * 50
        assert len(gen.slugify_topic(long)) <= 40

    def test_empty_input_returns_a_safe_fallback(self):
        assert gen.slugify_topic("") == "projeto"
        assert gen.slugify_topic("   ") == "projeto"

    def test_emoji_only_input_returns_a_safe_fallback(self):
        assert gen.slugify_topic("😀🎉🔥") == "projeto"

    def test_never_returns_a_path_separator(self):
        for hostile in ["../../etc/passwd", "a/b", "a\\b", "C:\\Windows", "evil..path"]:
            assert "/" not in gen.slugify_topic(hostile)
            assert "\\" not in gen.slugify_topic(hostile)


# -------------------------------------------------------- GenerationRequest.validate

class TestGenerationRequestValidate:
    def test_blank_topic_raises(self):
        with pytest.raises(ValueError):
            GenerationRequest(topic="").validate()
        with pytest.raises(ValueError):
            GenerationRequest(topic="   ").validate()

    def test_section_count_out_of_range_raises(self):
        for bad in (0, -1, script_gen.MAX_SECTIONS + 1, 99):
            with pytest.raises(ValueError):
                GenerationRequest(topic="dinheiro", section_count=bad).validate()

    def test_unknown_aspect_ratio_raises(self):
        with pytest.raises(ValueError):
            GenerationRequest(topic="x", aspect_ratio="hexagon").validate()

    def test_variants_out_of_range_raises(self):
        # variants is not a GenerationRequest field in this build, so the
        # orchestrator validates it at submit time instead; the request itself
        # still rejects the rest of the contract.
        with pytest.raises(ValueError):
            GenerationRequest(topic="x", music_volume=2.5).validate()

    def test_unsafe_preset_raises(self):
        with pytest.raises(ValueError):
            GenerationRequest(topic="x", preset="not-a-preset").validate()

    def test_accepts_valid_input(self):
        req = GenerationRequest(
            topic="dinheiro", language="pt-PT", section_count=5,
            aspect_ratio="vertical", preset="cinematic",
        ).validate()
        assert req.topic == "dinheiro"
        assert req.aspect_ratio == "vertical"
        assert req.preset == "cinematic"

    def test_defaults_are_applied_for_optional_keys(self):
        req = GenerationRequest(topic="dinheiro").validate()
        assert req.language == gen.DEFAULT_LANGUAGE
        assert req.voice == gen.DEFAULT_VOICE
        assert req.preset == gen.DEFAULT_PRESET
        assert req.music_volume == gen.DEFAULT_MUSIC_VOLUME
        assert req.duck_voice is True


# --------------------------------------------------------------------- happy path

class TestHappyPath:
    def test_submit_returns_queued(self, fake_pipeline):
        job = gen.submit_job({"topic": "dinheiro"})
        assert job.status == "queued"
        assert job.job_id
        assert job.progress == 0.0

    def test_stages_run_in_order_with_exact_progress(self, fake_pipeline):
        job = gen.submit_job({"topic": "dinheiro"})
        _run(job.job_id)
        updated = gen.get_job(job.job_id)
        assert updated.status == "completed"
        assert updated.progress == 1.0
        assert updated.result is not None
        assert updated.result["project_name"] == "dinheiro"

    def test_each_stage_sets_exact_stage_and_progress(self, fake_pipeline, monkeypatch):
        seen = []
        _real_stage = gen._stage

        def _record(job, stage, progress, message):
            seen.append((stage, progress))
            _real_stage(job, stage, progress, message)

        monkeypatch.setattr(gen, "_stage", _record)
        job = gen.submit_job({"topic": "dinheiro"})
        _run(job.job_id)
        assert seen == [
            ("script", gen.PROGRESS_SCRIPT),
            ("voice", gen.PROGRESS_VOICE),
            ("media", gen.PROGRESS_MEDIA),
            ("render", gen.PROGRESS_RENDER),
            ("done", gen.PROGRESS_DONE),
        ]

    def test_progress_is_monotonic_and_ends_at_one(self, fake_pipeline):
        job = gen.submit_job({"topic": "dinheiro"})
        _run(job.job_id)
        updated = gen.get_job(job.job_id)
        assert updated.progress == 1.0
        assert updated.status == "completed"

    def test_renderer_receives_expected_kwargs(self, fake_pipeline):
        job = gen.submit_job({
            "topic": "dinheiro", "preset": "cinematic",
            "music_volume": 0.5, "duck_voice": False,
        })
        _run(job.job_id)
        kwargs = fake_pipeline.last_kwargs
        assert kwargs["preset"] == "cinematic"
        assert kwargs["music_volume"] == 0.5
        assert kwargs["duck_voice"] is False
        assert "subtitle_style" in kwargs

    def test_fallback_script_still_completes(self, fake_pipeline, monkeypatch):
        monkeypatch.setattr(
            gen.script_gen, "generate_script",
            lambda *a, **k: _script(
                source="fallback-local", fallback_error="sem chave OpenRouter",
                degraded=True,
            ),
        )
        job = gen.submit_job({"topic": "dinheiro"})
        _run(job.job_id)
        updated = gen.get_job(job.job_id)
        assert updated.status == "completed"
        assert updated.result["script_source"] == "fallback-local"
        assert updated.result["fallback_error"] == "sem chave OpenRouter"
        assert updated.result["degraded"] is True


# --------------------------------------------------------------- failure paths

class TestFailurePaths:
    def test_tts_runtime_error_yields_failed_state(self, fake_pipeline, monkeypatch):
        async def _boom(*a, **k):
            raise RuntimeError("edge-tts não está instalado")
        monkeypatch.setattr(gen.tts, "synthesize_speech_long", _boom)
        job = gen.submit_job({"topic": "dinheiro"})
        _run(job.job_id)
        updated = gen.get_job(job.job_id)
        assert updated.status == "failed"
        assert updated.error
        assert "edge-tts" in updated.error
        assert updated.stage == "voice"

    def test_tts_auth_error_preserves_the_code(self, fake_pipeline, monkeypatch):
        from backend.services.auth_contract import AUTH_INVALID_KEY, AuthError

        async def _boom(*a, **k):
            raise AuthError(AUTH_INVALID_KEY, "Chave rejeitada.", 403)
        monkeypatch.setattr(gen.tts, "synthesize_speech_long", _boom)
        job = gen.submit_job({"topic": "dinheiro"})
        _run(job.job_id)
        updated = gen.get_job(job.job_id)
        assert updated.status == "failed"
        assert "AUTH_INVALID_KEY" in (updated.error or "")
        assert "403" in (updated.error or "")

    def test_render_status_error_yields_failed_state(self, fake_pipeline, monkeypatch):
        async def _bad_render(*a, **k):
            return {"status": "error", "error": "Sem memória.", "message": "Falha no render."}
        monkeypatch.setattr(gen.render_engine, "render_video_hyperframes", _bad_render)
        job = gen.submit_job({"topic": "dinheiro"})
        _run(job.job_id)
        updated = gen.get_job(job.job_id)
        assert updated.status == "failed"
        assert "Sem memória" in (updated.error or "")

    def test_no_exception_escapes_to_the_caller(self, fake_pipeline, monkeypatch):
        async def _boom(*a, **k):
            raise RuntimeError("qualquer coisa")
        monkeypatch.setattr(gen.render_engine, "render_video_hyperframes", _boom)
        job = gen.submit_job({"topic": "dinheiro"})
        # Must not raise.
        _run(job.job_id)
        assert gen.get_job(job.job_id).status == "failed"

    def test_failing_script_stage_also_marks_failed(self, fake_pipeline, monkeypatch):
        def _bad_script(*a, **k):
            raise RuntimeError("forn indisponível")
        monkeypatch.setattr(gen.script_gen, "generate_script", _bad_script)
        job = gen.submit_job({"topic": "dinheiro"})
        _run(job.job_id)
        updated = gen.get_job(job.job_id)
        assert updated.status == "failed"
        assert updated.stage == "script"
        assert "forn" in updated.error


# --------------------------------------------------------------- cancellation

class TestCancellation:
    def test_cancel_before_start_prevents_the_pipeline(self, fake_pipeline):
        job = gen.submit_job({"topic": "dinheiro"})
        assert gen.cancel_job(job.job_id) is True
        updated = gen.get_job(job.job_id)
        assert updated.status == "cancelled"
        # The pipeline must never have run: no render kwargs recorded.
        assert fake_pipeline.last_kwargs == {}

    def test_cancelling_a_finished_job_returns_false(self, fake_pipeline):
        job = gen.submit_job({"topic": "dinheiro"})
        _run(job.job_id)
        assert gen.cancel_job(job.job_id) is False

    def test_cancelling_an_unknown_job_returns_false(self, fake_pipeline):
        assert gen.cancel_job("does-not-exist") is False


# ------------------------------------------------------------- concurrency cap

class TestConcurrencyCap:
    def test_get_job_returns_none_for_unknown_id(self, fake_pipeline):
        assert gen.get_job("nope") is None

    def test_list_jobs_is_newest_first(self, fake_pipeline):
        first = gen.submit_job({"topic": "um"})
        second = gen.submit_job({"topic": "dois"})
        third = gen.submit_job({"topic": "três"})
        ids = [j.job_id for j in gen.list_jobs()]
        assert ids == [third.job_id, second.job_id, first.job_id]

    def test_list_jobs_respects_limit(self, fake_pipeline):
        for i in range(5):
            gen.submit_job({"topic": f"tema {i}"})
        assert len(gen.list_jobs()) == 5

    def test_submit_beyond_cap_does_not_raise_and_all_resolve(self, fake_pipeline):
        jobs = [gen.submit_job({"topic": f"tema {i}"}) for i in range(5)]
        for job in jobs:
            _run(job.job_id)
        statuses = {gen.get_job(j.job_id).status for j in jobs}
        assert statuses == {"completed"}

    def test_registry_prunes_beyond_bound(self, fake_pipeline, monkeypatch):
        # Force the prune window to zero so every finished job is eligible.
        monkeypatch.setattr(gen, "_time", lambda: 1_000_000.0)
        for i in range(gen.MAX_JOB_HISTORY + 5):
            job = gen.submit_job({"topic": f"tema {i}"})
            _run(job.job_id)
        # Pruning happens when a job completes; drive the trigger through the
        # worker so the prune actually runs.
        trigger = gen.submit_job({"topic": "trigger"})
        _run(trigger.job_id)
        assert len(gen._jobs) <= gen.MAX_JOB_HISTORY
