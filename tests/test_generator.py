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

    def _assert_no_transcribe(path):
        raise AssertionError("the generator must not transcribe audio whose text it already has")
    monkeypatch.setattr(gen.pipeline, "transcribe_audio_file", _assert_no_transcribe)
    monkeypatch.setattr(gen.pipeline, "get_media_duration", lambda path: 12.0)
    monkeypatch.setattr(gen.pipeline, "search_media_for_scenes", lambda sb, **kw: ({}, "none"))
    monkeypatch.setattr(gen.pipeline, "extract_keywords_from_text", lambda text: ["dinheiro"])
    monkeypatch.setattr(gen.pipeline, "search_media_for_keywords", lambda kw: [])
    monkeypatch.setattr(gen.music, "pick_track", lambda mood, exclude_ids=None: None)

    async def _fake_render(*args, **kwargs):
        _fake_render.last_kwargs = dict(kwargs)
        await asyncio.sleep(0)  # yield so tests can observe RUNNING state
        output_stem = kwargs.get("output_stem", "projeto")
        output_path = tmp_path / f"{output_stem}.mp4"
        output_path.write_bytes(b"fake-mp4-bytes")
        return {
            "project_name": output_stem,
            "output_stem": output_stem,
            "status": "rendered",
            "output_path": str(output_path),
            "storyboard": _storyboard(),
            "scene_count": 3,
            "scenes_with_media": 3,
            "preset": kwargs.get("preset", "cinematic"),
            "message": "Vídeo renderizado com HyperFrames com sucesso.",
        }

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
        # New lifecycle includes an initial "starting" stage before the actual script stage
        assert seen == [
            ("script", gen.PROGRESS_SCRIPT),  # "A iniciar..." - job becomes RUNNING
            ("script", gen.PROGRESS_SCRIPT),  # "A gerar o guião..." - actual script generation
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



# --------------------------------------------------------------- running lifecycle

class TestRunningLifecycle:
    def test_job_stays_queued_while_waiting_for_slot(self, fake_pipeline, monkeypatch):
        """A job behind the concurrency cap stays QUEUED until it acquires a slot."""
        # Reduce concurrency to 1 to make queuing deterministic
        monkeypatch.setattr(gen, "MAX_CONCURRENT_JOBS", 1)
        gen.reset_for_tests()

        # Submit two jobs; the second must wait
        job1 = gen.submit_job({"topic": "primeiro"})
        job2 = gen.submit_job({"topic": "segundo"})

        # Both start as queued
        assert gen.get_job(job1.job_id).status == "queued"
        assert gen.get_job(job2.job_id).status == "queued"

        # Run both jobs concurrently in the same event loop
        async def run_both():
            task1 = asyncio.create_task(gen._run_job(job1.job_id))
            # Give job1 time to acquire the slot and become RUNNING
            for _ in range(50):
                j1 = gen.get_job(job1.job_id)
                if j1 and j1.status == "running":
                    break
                await asyncio.sleep(0)
            else:
                raise AssertionError("job1 did not become running")

            # job1 should now be RUNNING, job2 should still be QUEUED
            assert gen.get_job(job1.job_id).status == "running"
            assert gen.get_job(job2.job_id).status == "queued"

            # Start job2 (it will queue behind job1)
            task2 = asyncio.create_task(gen._run_job(job2.job_id))

            # Wait for job1 to complete
            await task1
            assert gen.get_job(job1.job_id).status == "completed"

            # Now job2 should be able to acquire the slot
            for _ in range(50):
                j2 = gen.get_job(job2.job_id)
                if j2 and j2.status == "running":
                    break
                await asyncio.sleep(0)
            else:
                raise AssertionError("job2 did not become running after job1 completed")

            assert gen.get_job(job2.job_id).status == "running"

            # Wait for job2 to complete
            await task2
            assert gen.get_job(job2.job_id).status == "completed"

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(run_both())
        finally:
            loop.close()

    def test_at_most_max_concurrent_jobs_running(self, fake_pipeline, monkeypatch):
        """With MAX_CONCURRENT_JOBS = 2, at most 2 jobs are RUNNING simultaneously."""
        monkeypatch.setattr(gen, "MAX_CONCURRENT_JOBS", 2)
        gen.reset_for_tests()

        running_counts = []

        # Wrap _run_job to track concurrent running jobs
        original_run_job = gen._run_job

        async def tracking_run_job(job_id):
            running_counts.append(len(gen._running_ids))
            return await original_run_job(job_id)

        monkeypatch.setattr(gen, "_run_job", tracking_run_job)

        # Submit 5 jobs and run them all concurrently
        jobs = [gen.submit_job({"topic": f"tema {i}"}) for i in range(5)]

        async def run_all():
            tasks = [asyncio.create_task(gen._run_job(job.job_id)) for job in jobs]
            await asyncio.gather(*tasks)

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(run_all())
        finally:
            loop.close()

        # Check that at no point were more than 2 jobs running
        assert all(count <= 2 for count in running_counts), f"Concurrency violated: {running_counts}"
        # At least once we should have seen 2 running (when jobs overlap)
        assert max(running_counts) == 2, f"Expected peak concurrency of 2, got {max(running_counts)}"

    def test_cancel_job_returns_false_for_running_job(self, fake_pipeline, monkeypatch):
        """cancel_job returns False for a RUNNING job and does not corrupt state."""
        monkeypatch.setattr(gen, "MAX_CONCURRENT_JOBS", 1)
        gen.reset_for_tests()

        job = gen.submit_job({"topic": "dinheiro"})

        async def run_and_test_cancel():
            task = asyncio.create_task(gen._run_job(job.job_id))

            # Wait for it to become RUNNING
            for _ in range(50):
                j = gen.get_job(job.job_id)
                if j and j.status == "running":
                    break
                await asyncio.sleep(0)
            else:
                raise AssertionError("job did not become running")

            # Try to cancel - should return False
            assert gen.cancel_job(job.job_id) is False

            # Job should still be RUNNING (not corrupted)
            assert gen.get_job(job.job_id).status == "running"

            # Wait for completion
            await task

            # Job should complete normally
            assert gen.get_job(job.job_id).status == "completed"

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(run_and_test_cancel())
        finally:
            loop.close()

    def test_happy_path_passes_through_running(self, fake_pipeline):
        """The happy path goes QUEUED -> RUNNING -> COMPLETED."""
        job = gen.submit_job({"topic": "dinheiro"})
        assert job.status == "queued"

        async def run_and_check():
            task = asyncio.create_task(gen._run_job(job.job_id))

            # Wait for RUNNING - check immediately, then poll
            for _ in range(50):
                j = gen.get_job(job.job_id)
                if j and j.status == "running":
                    break
                await asyncio.sleep(0)  # yield immediately to let task run
            else:
                raise AssertionError("job did not become running")

            assert gen.get_job(job.job_id).status == "running"

            # Wait for completion
            await task

            assert gen.get_job(job.job_id).status == "completed"

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(run_and_check())
        finally:
            loop.close()

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

class TestEventLoopLiveness:
    def test_no_blocking_work_runs_on_loop_thread(self, fake_pipeline, monkeypatch):
        """Every blocking stage must execute in a worker thread, not on the loop."""
        import threading
        recorded: list = []
        loop_name = threading.current_thread().name

        def _recorder(name):
            def decorator(fn):
                def wrapper(*a, **k):
                    recorded.append((name, threading.current_thread().name))
                    return fn(*a, **k)
                return wrapper
            return decorator

        monkeypatch.setattr(
            gen.script_gen, "generate_script",
            _recorder("script")(gen.script_gen.generate_script),
        )

        async def _recorder_tts(*a, **k):
            recorded.append(("tts", threading.current_thread().name))
            return await gen.tts.synthesize_speech_long(*a, **k)
        monkeypatch.setattr(gen.tts, "synthesize_speech_long", _recorder_tts)

        async def _recorder_render(*a, **k):
            recorded.append(("render", threading.current_thread().name))
            return await gen.render_engine.render_video_hyperframes(*a, **k)
        monkeypatch.setattr(gen.render_engine, "render_video_hyperframes", _recorder_render)

        original_search_scenes = gen.pipeline.search_media_for_scenes
        def _recorder_search_scenes(*a, **k):
            recorded.append(("search_scenes", threading.current_thread().name))
            return original_search_scenes(*a, **k)
        monkeypatch.setattr(gen.pipeline, "search_media_for_scenes", _recorder_search_scenes)

        original_search_keywords = gen.pipeline.search_media_for_keywords
        def _recorder_search_keywords(*a, **k):
            recorded.append(("search_keywords", threading.current_thread().name))
            return original_search_keywords(*a, **k)
        monkeypatch.setattr(gen.pipeline, "search_media_for_keywords", _recorder_search_keywords)

        job = gen.submit_job({"topic": "dinheiro"})
        _run(job.job_id)

        assert recorded, "No blocking stages were recorded"
        for stage, thread_name in recorded:
            assert thread_name != loop_name, f"{stage} ran on loop thread {thread_name}"


class TestOutputUniqueness:
    def test_same_topic_jobs_get_different_stems_and_paths(self, fake_pipeline):
        """Two same-topic jobs with no prefix must write different paths."""
        jobs = [gen.submit_job({"topic": "a-historia-da-navegacao-portuguesa"}) for _ in range(2)]

        async def run_both():
            tasks = [asyncio.create_task(gen._run_job(j.job_id)) for j in jobs]
            await asyncio.gather(*tasks)

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(run_both())
        finally:
            loop.close()

        results = [gen.get_job(j.job_id).result for j in jobs]
        stems = [r["output_stem"] for r in results]
        paths = [r["render"]["output_path"] for r in results]

        assert stems[0] != stems[1], "Stems must differ for concurrent same-topic jobs"
        assert paths[0] != paths[1], "Paths must differ for concurrent same-topic jobs"
        for path in paths:
            assert Path(path).exists(), f"Output file must exist: {path}"

    def test_explicit_project_prefix_wins(self, fake_pipeline):
        """An explicit project_prefix must still win and override the slug."""
        job = gen.submit_job({
            "topic": "a-historia-da-navegacao-portuguesa",
            "project_prefix": "meu-projeto",
        })
        _run(job.job_id)
        result = gen.get_job(job.job_id).result
        assert result["output_stem"] == "meu-projeto"


class TestDegradedCaptions:
    def test_degraded_true_when_captions_placeholder_regardless_of_script(self, fake_pipeline, monkeypatch):
        """degraded must be true whenever captions are a placeholder."""
        monkeypatch.setattr(
            gen.script_gen, "generate_script",
            lambda *a, **k: _script(source="openrouter-free", degraded=False),
        )
        monkeypatch.setattr(
            gen.pipeline, "transcription_status",
            lambda segs: {
                "transcript_source": "placeholder",
                "degraded": True,
                "fallback_error": "As legendas são um placeholder.",
                "warning": "As legendas são um placeholder.",
            },
        )
        job = gen.submit_job({"topic": "dinheiro"})
        _run(job.job_id)
        result = gen.get_job(job.job_id).result
        assert result["degraded"] is True
        assert result["fallback_error"] == "As legendas são um placeholder."

    def test_degraded_false_when_script_good_and_captions_ok(self, fake_pipeline):
        """degraded is false only when both script and captions are fine."""
        job = gen.submit_job({"topic": "dinheiro"})
        _run(job.job_id)
        result = gen.get_job(job.job_id).result
        assert result["degraded"] is False


class TestCaptionsDeriveFromText:
    def test_srt_and_storyboard_derive_from_full_text(self, fake_pipeline):
        """The SRT segments and storyboard captions must be substrings of the narration."""
        job = gen.submit_job({"topic": "dinheiro"})
        _run(job.job_id)
        result = gen.get_job(job.job_id).result
        full_text = result["script"]["full_text"]
        for scene in result["storyboard"]:
            caption = scene.get("caption", "")
            assert caption, "storyboard caption must not be empty"
            assert caption in full_text, f"Caption '{caption}' is not a substring of the narration"
        for seg in result["segments"]:
            text = seg.get("text", "")
            assert text, "segment text must not be empty"
            assert text in full_text, f"Segment '{text}' is not a substring of the narration"


def _music_track(track_id="dark-depths", mood="dark"):
    """A real music.Track for the pick_track stand-ins.

    Real dataclass rather than a Mock: run_generation reads .id off the result,
    so a stub that only answers the attributes a test happens to look at would
    let a regression in the surrounding plumbing through unnoticed.
    """
    return gen.music.Track(
        id=track_id,
        title="Dark Depths",
        mood=mood,
        duration=40.0,
        path="/tmp/" + track_id + ".wav",
        builtin=True,
        source="builtin",
    )


class TestMusicTrackSelection:
    """Which track reaches the render, and where pick_track runs.

    Two rules are pinned here. An explicit music_track wins outright and the
    catalogue is never consulted for it. Without one, pick_track is consulted
    with the requested mood - and it runs in a worker thread, because it
    synthesises the builtin library on a fresh checkout (~5.6 s), the stall
    that froze GET /api/jobs/{id} for 4.176 s the last time it sat on the loop.
    """

    def test_explicit_music_track_reaches_render_without_pick_track(self, fake_pipeline, monkeypatch):
        """An explicit music_track is passed straight through to the render.

        Catches a regression where mood selection starts overriding what the
        caller asked for, or where pick_track starts running even though a track
        was requested.
        """
        asked = []

        def _pick(mood, exclude_ids=None):
            asked.append(mood)
            return _music_track("ambient-drift", "ambient")

        monkeypatch.setattr(gen.music, "pick_track", _pick)

        job = gen.submit_job({"topic": "dinheiro", "music_track": "lofi-nightfall"})
        _run(job.job_id)

        updated = gen.get_job(job.job_id)
        assert updated.status == "completed", updated.error
        assert asked == [], "pick_track must not be consulted for an explicit track"
        assert fake_pipeline.last_kwargs["music_track"] == "lofi-nightfall"
        assert updated.result["music_track"] == "lofi-nightfall"

    def test_mood_selection_is_used_when_no_explicit_track(self, fake_pipeline, monkeypatch):
        """With no explicit track, pick_track decides and the render hears it.

        Catches a regression where the mood argument is dropped or hardcoded,
        where the chosen id never reaches the render, or where pick_track is
        skipped and the job renders silently.
        """
        asked = []

        def _pick(mood, exclude_ids=None):
            asked.append((mood, exclude_ids))
            return _music_track("dark-depths", "dark")

        monkeypatch.setattr(gen.music, "pick_track", _pick)

        job = gen.submit_job({"topic": "dinheiro", "music_mood": "dark"})
        _run(job.job_id)

        updated = gen.get_job(job.job_id)
        assert updated.status == "completed", updated.error
        assert asked == [("dark", None)], "pick_track was not asked for the requested mood"
        assert fake_pipeline.last_kwargs["music_track"] == "dark-depths"
        assert updated.result["music_track"] == "dark-depths"

    def test_slow_pick_track_leaves_the_event_loop_free(self, fake_pipeline, monkeypatch):
        """A slow pick_track must not park the loop it is called from.

        Regression guard for the ~5.6 s library synthesis a fresh checkout pays
        inside pick_track. The stand-in blocks for half a second and records the
        thread it ran on plus how many times the loop kept turning while it
        did: called straight from the coroutine the count is zero, and the
        WebUI progress poll stalls for the whole call.
        """
        import threading
        import time

        loop_thread = threading.current_thread().name
        observed = {}
        ticks = []

        def _slow_pick(mood, exclude_ids=None):
            observed["mood"] = mood
            observed["thread"] = threading.current_thread().name
            observed["ticks_before"] = len(ticks)
            deadline = time.monotonic() + 0.5
            while time.monotonic() < deadline:
                time.sleep(0.01)
            observed["ticks_during"] = len(ticks) - observed["ticks_before"]
            return _music_track("dark-depths", "dark")

        monkeypatch.setattr(gen.music, "pick_track", _slow_pick)

        job = gen.submit_job({"topic": "dinheiro", "music_mood": "dark"})

        async def _run_and_heartbeat():
            task = asyncio.create_task(gen._run_job(job.job_id))
            while not task.done():
                ticks.append(time.monotonic())
                await asyncio.sleep(0.005)
            await task

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_run_and_heartbeat())
        finally:
            loop.close()

        updated = gen.get_job(job.job_id)
        assert updated.status == "completed", updated.error
        assert observed, "pick_track was never consulted"
        assert observed["thread"] != loop_thread, (
            "pick_track ran on the event loop thread: " + observed["thread"]
        )
        assert observed["ticks_during"] >= 10, (
            "the loop stopped turning during pick_track: "
            + str(observed["ticks_during"]) + " ticks"
        )
        assert fake_pipeline.last_kwargs["music_track"] == "dark-depths"
