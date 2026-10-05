"""Regression tests for the media-in-the-video defects.

The render pipeline previously produced an MP4 made of flat background colours:
the composition generator emitted an empty media node for every image asset, and
even once the tags existed the assets sat outside the HyperFrames project
directory, so the renderer refused to load them. Every existing test still passed
because none of them looked at the generated composition.

These tests assert on the composition HTML itself - no render required - plus the
scene timing invariant that used to stack two captions on screen.
"""
import re

import pytest

from backend.services.music import missing_library_note
from backend.services.pipeline import (
    build_storyboard_from_segments,
    extract_keywords_from_text,
    normalize_scene_timings,
    parse_srt_to_segments,
    search_media_for_keywords,
)
from backend.services.render_engine import (
    IMAGE_EXTENSIONS,
    VIDEO_EXTENSIONS,
    create_project_dir,
    escape_html,
    generate_composition_html,
    get_dimensions,
    stage_project_assets,
)


# A 1x1 PNG and a 1x1 MP4-ish payload are enough: the composition only cares that
# a real file with the right extension was staged.
PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000a49444154789c6360000002000100ffff0300000600"
    "0557bfabd40000000049454e44ae426082"
)
MP4_BYTES = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom"


def _segments(rows):
    return [
        {"index": i + 1, "start": start, "end": end, "text": text}
        for i, (start, end, text) in enumerate(rows)
    ]


def _stage(monkeypatch, tmp_path, storyboard, audio_name=None):
    """Build a composition project in a temp dir, never in the real OUTPUT_DIR."""
    import backend.services.render_engine as engine

    monkeypatch.setattr(engine, "OUTPUT_DIR", tmp_path)
    project_dir = create_project_dir("pytest_media")
    media_dir = project_dir / "assets"
    media_dir.mkdir(parents=True, exist_ok=True)
    for idx in range(len(storyboard)):
        ext = ".png" if idx % 2 == 0 else ".mp4"
        (media_dir / f"scene_{idx}{ext}").write_bytes(PNG_BYTES if ext == ".png" else MP4_BYTES)
    audio_path = None
    if audio_name:
        audio_path = project_dir / audio_name
        audio_path.write_bytes(PNG_BYTES)
    refs = {idx: f"assets/scene_{idx}{'.png' if idx % 2 == 0 else '.mp4'}" for idx in range(len(storyboard))}
    return project_dir, refs, ("assets/" + audio_name if audio_name else None)


class TestCompositionContainsMedia:
    def test_image_assets_become_img_tags(self, monkeypatch, tmp_path):
        storyboard = build_storyboard_from_segments(_segments([(0.0, 4.0, "primeira cena")]))
        _, refs, _ = _stage(monkeypatch, tmp_path, storyboard)
        html = generate_composition_html("p", storyboard, refs, None, "vertical", 4.0)
        assert "<img" in html, "image asset produced no <img> tag"
        assert 'src="assets/scene_0.png"' in html

    def test_video_assets_become_video_tags(self, monkeypatch, tmp_path):
        rows = [(0.0, 4.0, "a"), (4.0, 8.0, "b")]
        storyboard = build_storyboard_from_segments(_segments(rows))
        _, refs, _ = _stage(monkeypatch, tmp_path, storyboard)
        html = generate_composition_html("p", storyboard, refs, None, "vertical", 8.0)
        assert "<video" in html, "video asset produced no <video> tag"
        # Video clips must stay muted and inline for the renderer to accept them.
        assert "muted" in html and "playsinline" in html

    def test_scenes_without_media_still_render(self, monkeypatch, tmp_path):
        storyboard = build_storyboard_from_segments(_segments([(0.0, 4.0, "sem media")]))
        html = generate_composition_html("p", storyboard, {}, None, "vertical", 4.0)
        assert 'class="clip scene"' in html, "a scene with no asset must still be emitted"

    def test_every_staged_asset_is_referenced(self, monkeypatch, tmp_path):
        rows = [(0.0, 3.0, "a"), (3.0, 6.0, "b"), (6.0, 9.0, "c")]
        storyboard = build_storyboard_from_segments(_segments(rows))
        _, refs, _ = _stage(monkeypatch, tmp_path, storyboard)
        html = generate_composition_html("p", storyboard, refs, None, "vertical", 9.0)
        for ref in refs.values():
            assert ref in html, f"staged asset {ref} never referenced"

    def test_no_absolute_paths_leak_into_the_composition(self, monkeypatch, tmp_path):
        rows = [(0.0, 3.0, "a"), (3.0, 6.0, "b")]
        storyboard = build_storyboard_from_segments(_segments(rows))
        _, refs, audio = _stage(monkeypatch, tmp_path, storyboard, audio_name="narration.mp3")
        html = generate_composition_html("p", storyboard, refs, audio, "vertical", 6.0)
        # The renderer blocks local resources outside its own project directory,
        # so any absolute path here means the asset will silently fail to load.
        assert not re.search(r'src="[A-Za-z]:[/\\]', html)
        assert not re.search(r'src="file:', html)


class TestCompositionContract:
    def test_root_declares_canvas_and_duration(self, monkeypatch, tmp_path):
        storyboard = build_storyboard_from_segments(_segments([(0.0, 5.0, "a")]))
        html = generate_composition_html("p", storyboard, {}, None, "vertical", 5.0)
        width, height = get_dimensions("vertical")
        assert f'data-composition-id="main"' in html
        assert f'data-width="{width}"' in html
        assert f'data-height="{height}"' in html
        assert 'data-duration="5.0"' in html

    def test_timeline_is_paused_and_registered(self, monkeypatch, tmp_path):
        storyboard = build_storyboard_from_segments(_segments([(0.0, 5.0, "a")]))
        html = generate_composition_html("p", storyboard, {}, None, "vertical", 5.0)
        assert "gsap.timeline({ paused: true })" in html
        assert 'window.__timelines["main"] = tl' in html

    def test_no_repeating_timeline(self, monkeypatch, tmp_path):
        storyboard = build_storyboard_from_segments(_segments([(0.0, 5.0, "a")]))
        html = generate_composition_html("p", storyboard, {}, None, "vertical", 5.0)
        assert "repeat: -1" not in html, "an infinite timeline makes the render non-deterministic"

    def test_audio_is_timed_and_identified(self, monkeypatch, tmp_path):
        storyboard = build_storyboard_from_segments(_segments([(0.0, 5.0, "a")]))
        _, _, audio = _stage(monkeypatch, tmp_path, storyboard, audio_name="narration.mp3")
        html = generate_composition_html("p", storyboard, {}, audio, "vertical", 5.0)
        # An <audio> with src but no data-start makes preview and render diverge.
        assert 'id="main-audio"' in html
        assert re.search(r"<audio[^>]*data-start=", html), "<audio> is missing data-start"

    def test_caption_text_is_escaped(self, monkeypatch, tmp_path):
        storyboard = build_storyboard_from_segments(
            _segments([(0.0, 4.0, '<script>alert("x")</script>')])
        )
        html = generate_composition_html("p", storyboard, {}, None, "vertical", 4.0)
        assert "<script>alert" not in html
        assert "&lt;script&gt;" in html

    def test_background_colour_is_escaped_into_a_style_attribute(self, monkeypatch, tmp_path):
        storyboard = build_storyboard_from_segments(_segments([(0.0, 4.0, "a")]))
        storyboard[0]["background"] = '#0f172a" onload="alert(1)'
        html = generate_composition_html("p", storyboard, {}, None, "vertical", 4.0)
        assert 'onload="alert(1)"' not in html

    def test_no_body_background_animation(self, monkeypatch, tmp_path):
        """Animating the CSS `background` shorthand silently does nothing."""
        storyboard = build_storyboard_from_segments(_segments([(0.0, 4.0, "a")]))
        html = generate_composition_html("p", storyboard, {}, None, "vertical", 4.0)
        assert 'tl.to("body"' not in html

    def test_animations_target_inner_nodes_not_clips(self, monkeypatch, tmp_path):
        """A .clip is owned by the runtime; tweening it is rejected by lint."""
        storyboard = build_storyboard_from_segments(_segments([(0.0, 4.0, "a")]))
        html = generate_composition_html("p", storyboard, {}, None, "vertical", 4.0)
        assert not re.search(r'tl\.fromTo\("\.clip', html)


class TestSceneTimingIsSequential:
    def test_consecutive_scenes_overlap_by_exactly_the_transition_window(self):
        """Scenes overlap so a cross-wipe has the outgoing scene behind it."""
        from backend.services.pipeline import SCENE_OVERLAP

        rows = [(0.0, 6.5, "a"), (6.5, 12.5, "b"), (12.5, 14.9, "c"), (14.9, 22.0, "d")]
        storyboard = build_storyboard_from_segments(_segments(rows))
        for previous, current in zip(storyboard, storyboard[1:]):
            overlap = previous["end"] - current["start"]
            assert overlap >= 0, "a gap between scenes loses the transition"
            assert overlap <= SCENE_OVERLAP + 1e-6, (
                f"scenes overlap {overlap:.2f}s; the transition window is {SCENE_OVERLAP}s"
            )

    def test_the_first_scene_starts_at_zero(self):
        storyboard = build_storyboard_from_segments(_segments([(2.0, 5.5, "a")]))
        assert storyboard[0]["start"] == 0.0

    def test_source_timings_are_kept_for_audit(self):
        rows = [(0.0, 6.5, "a"), (6.5, 12.5, "b")]
        storyboard = build_storyboard_from_segments(_segments(rows))
        assert storyboard[0]["source_start"] == pytest.approx(0.0)
        assert storyboard[1]["source_start"] == pytest.approx(6.5)
        assert storyboard[1]["source_end"] == pytest.approx(12.5)

    def test_later_scenes_start_early_but_never_before_their_predecessor(self):
        rows = [(0.0, 6.5, "a"), (6.5, 12.5, "b")]
        storyboard = build_storyboard_from_segments(_segments(rows))
        first, second = storyboard
        assert second["start"] < first["end"], "the incoming scene must overlap the outgoing"
        assert second["start"] >= first["start"], "degenerate stacking is never produced"

    def test_duration_matches_end_minus_start(self):
        storyboard = build_storyboard_from_segments(_segments([(2.0, 5.5, "a")]))
        scene = storyboard[0]
        assert scene["duration"] == pytest.approx(scene["end"] - scene["start"])

    def test_zero_length_segment_gets_a_usable_duration(self):
        storyboard = build_storyboard_from_segments(_segments([(0.0, 0.0, "a")]))
        assert storyboard[0]["duration"] > 0.0

    def test_very_short_scene_is_floored_but_stays_inside_its_slot(self):
        storyboard = normalize_scene_timings(
            [{"index": 1, "start": 0.0, "end": 0.1}, {"index": 2, "start": 5.0, "end": 9.0}],
            min_duration=0.8,
        )
        assert storyboard[0]["duration"] >= 0.8
        assert storyboard[0]["end"] <= storyboard[1]["start"]

    def test_normalizer_does_not_mutate_input(self):
        original = [{"index": 1, "start": 0.0, "end": 0.1, "duration": 99.0}]
        snapshot = dict(original[0])
        normalize_scene_timings(original, min_duration=0.8)
        assert original[0] == snapshot

    def test_empty_storyboard_is_handled(self):
        assert normalize_scene_timings([]) == []


class TestMediaSearchIsReal:
    def test_no_media_when_no_provider_is_enabled(self, monkeypatch):
        import backend.services.pipeline as pipeline

        for name in ("PEXELS_API_KEY", "PIXABAY_API_KEY"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setattr(pipeline, "get_provider_status", lambda: {
            "pexels": {"enabled": False}, "pixabay": {"enabled": False},
        })
        assert pipeline.search_media_for_keywords(["corrida"]) == [], (
            "fabricated media URLs are worse than none: they look real and are not"
        )

    def test_no_media_for_empty_keywords(self, monkeypatch):
        import backend.services.pipeline as pipeline

        monkeypatch.setattr(pipeline, "get_provider_status", lambda: {
            "pexels": {"enabled": True}, "pixabay": {"enabled": True},
        })
        assert pipeline.search_media_for_keywords([]) == []

    def test_results_come_from_the_provider_and_are_deduplicated(self, monkeypatch):
        import backend.services.pipeline as pipeline

        monkeypatch.setattr(pipeline, "get_provider_status", lambda: {
            "pexels": {"enabled": True}, "pixabay": {"enabled": False},
        })
        calls = []

        def fake_fetch(query, provider="pexels"):
            calls.append((query, provider))
            return [
                {"url": "https://example.test/a.jpg", "source": "Pexels", "kind": "image"},
                {"url": "https://example.test/a.jpg", "source": "Pexels", "kind": "image"},
                {"url": "https://example.test/b.jpg", "source": "Pexels", "kind": "image"},
            ]

        monkeypatch.setattr(pipeline, "fetch_provider_media", fake_fetch)
        pipeline._MEDIA_CACHE.clear()

        result = pipeline.search_media_for_keywords(["corrida", "musica"])
        urls = [item["url"] for item in result]
        assert urls == ["https://example.test/a.jpg", "https://example.test/b.jpg"]
        assert calls == [("corrida", "pexels"), ("musica", "pexels")], (
            "each keyword must reach the provider exactly once"
        )

    def test_second_call_is_served_from_cache(self, monkeypatch):
        import backend.services.pipeline as pipeline

        monkeypatch.setattr(pipeline, "get_provider_status", lambda: {
            "pexels": {"enabled": True}, "pixabay": {"enabled": False},
        })
        calls = []

        def fake_fetch(query, provider="pexels"):
            calls.append(query)
            return [{"url": f"https://example.test/{query}.jpg", "source": "Pexels", "kind": "image"}]

        monkeypatch.setattr(pipeline, "fetch_provider_media", fake_fetch)
        pipeline._MEDIA_CACHE.clear()

        pipeline.search_media_for_keywords(["corrida"])
        pipeline.search_media_for_keywords(["corrida"])
        assert calls == ["corrida"], "cached keywords must not re-hit the provider API"

    def test_fetch_provider_media_disabled_provider_returns_empty(self, monkeypatch):
        import backend.services.pipeline as pipeline

        monkeypatch.setattr(pipeline, "get_provider_status", lambda: {"pexels": {"enabled": False}})
        assert pipeline.fetch_provider_media("corrida", "pexels") == []

    def test_fetch_provider_media_blank_query_returns_empty(self, monkeypatch):
        import backend.services.pipeline as pipeline

        monkeypatch.setattr(pipeline, "get_provider_status", lambda: {"pexels": {"enabled": True}})
        assert pipeline.fetch_provider_media("   ", "pexels") == []


class TestAssetStaging:
    def test_stage_copies_assets_into_the_project_directory(self, monkeypatch, tmp_path):
        from backend.services.pipeline import STORAGE_DIR

        storyboard = [{"index": 1, "start": 0.0, "end": 4.0, "duration": 4.0, "media_url": "x"}]
        project_dir = tmp_path / "proj"
        (project_dir / "assets").mkdir(parents=True)

        staged_source = STORAGE_DIR / "media" / "pytest_stage"
        staged_source.mkdir(parents=True, exist_ok=True)
        (staged_source / "scene_0.png").write_bytes(PNG_BYTES)

        import backend.services.render_engine as engine

        original = engine.download_media_asset
        engine.download_media_asset = lambda *a, **k: staged_source / "scene_0.png"
        try:
            refs, audio, rejected = engine.stage_project_assets(
                "pytest_stage", storyboard, None, project_dir / "assets"
            )
        finally:
            engine.download_media_asset = original

        assert refs == {0: "assets/scene_0.png"}
        assert (project_dir / "assets" / "scene_0.png").exists()
        assert audio is None
        assert rejected == []

    def test_stage_skips_scenes_without_media(self, monkeypatch, tmp_path):
        import backend.services.render_engine as engine

        project_dir = tmp_path / "proj2"
        (project_dir / "assets").mkdir(parents=True)
        refs, audio, rejected = engine.stage_project_assets(
            "pytest_stage2",
            [{"index": 1, "start": 0.0, "end": 4.0, "duration": 4.0, "media_url": ""}],
            None,
            project_dir / "assets",
        )
        assert refs == {}
        assert audio is None
        assert rejected == []


class TestPaddedAssetRejection:
    """A stock image wrapped in a white canvas renders as a broken frame.

    object-fit: cover reproduces the canvas faithfully, so the asset has to be
    swapped out rather than trusted.
    """

    def _stage(self, monkeypatch, tmp_path, padded: bool, pool):
        import backend.services.render_engine as engine

        storyboard = [{
            "index": 1, "start": 0.0, "end": 4.0, "duration": 4.0,
            "media_url": "https://example.test/first.jpg",
        }]
        project_dir = tmp_path / "proj"
        (project_dir / "assets").mkdir(parents=True)

        good = tmp_path / "good.jpg"
        good.write_bytes(PNG_BYTES)

        calls = []

        def fake_download(url, project, idx):
            calls.append(url)
            return good

        # Only the first candidate is a padded canvas; the substitute is clean.
        monkeypatch.setattr(engine, "download_media_asset", fake_download)
        monkeypatch.setattr(engine, "looks_padded", lambda _path: len(calls) == 1 if padded else False)
        refs, _audio, rejected = engine.stage_project_assets(
            "padded_probe", storyboard, None, project_dir / "assets", pool
        )
        return refs, rejected, calls

    def test_padded_first_asset_is_replaced_by_the_next_candidate(self, monkeypatch, tmp_path):
        refs, rejected, calls = self._stage(
            monkeypatch, tmp_path, padded=True,
            pool=["https://example.test/first.jpg", "https://example.test/second.jpg"],
        )
        assert rejected and rejected[0]["reason"] == "padded canvas"
        assert len(calls) == 2, "the substitute candidate must actually be tried"
        assert refs == {0: "assets/good.jpg"}

    def test_a_clean_asset_is_accepted_first_time(self, monkeypatch, tmp_path):
        refs, rejected, calls = self._stage(
            monkeypatch, tmp_path, padded=False,
            pool=["https://example.test/other.jpg"],
        )
        assert rejected == []
        assert len(calls) == 1, "a good asset must not trigger extra downloads"
        assert refs == {0: "assets/good.jpg"}

    def test_scene_records_that_its_media_was_substituted(self, monkeypatch, tmp_path):
        import backend.services.render_engine as engine

        storyboard = [{
            "index": 1, "start": 0.0, "end": 4.0, "duration": 4.0,
            "media_url": "https://example.test/first.jpg",
        }]
        project_dir = tmp_path / "proj"
        (project_dir / "assets").mkdir(parents=True)
        good = tmp_path / "good.jpg"
        good.write_bytes(PNG_BYTES)

        calls = []
        monkeypatch.setattr(engine, "download_media_asset", lambda url, *a: (calls.append(url), good)[1])
        monkeypatch.setattr(engine, "looks_padded", lambda _p: len(calls) == 1)
        engine.stage_project_assets(
            "p", storyboard, None, project_dir / "assets",
            ["https://example.test/second.jpg"],
        )
        assert storyboard[0]["media_url"] == "https://example.test/second.jpg"
        assert storyboard[0]["media_substituted"] is True

    def test_scene_with_no_usable_candidate_ends_up_without_media(self, monkeypatch, tmp_path):
        import backend.services.render_engine as engine

        storyboard = [{
            "index": 1, "start": 0.0, "end": 4.0, "duration": 4.0,
            "media_url": "https://example.test/only.jpg",
        }]
        project_dir = tmp_path / "proj"
        (project_dir / "assets").mkdir(parents=True)
        good = tmp_path / "good.jpg"
        good.write_bytes(PNG_BYTES)

        monkeypatch.setattr(engine, "download_media_asset", lambda *a, **k: good)
        monkeypatch.setattr(engine, "looks_padded", lambda _p: True)
        refs, _audio, rejected = engine.stage_project_assets(
            "p", storyboard, None, project_dir / "assets", []
        )
        assert refs == {}, "an all-padded pool must leave the scene without media"
        assert len(rejected) == 1


class TestAssetCacheIsKeyedByUrl:
    """A cache keyed only by scene index breaks substitution and re-runs."""

    def test_two_urls_for_one_scene_get_separate_files(self, tmp_path, monkeypatch):
        import backend.services.pipeline as pipeline

        monkeypatch.setattr(pipeline, "STORAGE_DIR", tmp_path)

        class FakeResponse:
            def __init__(self, payload):
                self.content = payload

            def raise_for_status(self):
                return None

        def fake_get(url, **kwargs):
            marker = url.rsplit("/", 1)[-1][:4].encode()
            return FakeResponse(b"\xff\xd8\xff" + marker + b"a" * 2048)

        monkeypatch.setattr(pipeline.httpx, "get", fake_get)

        first = pipeline.download_media_asset("https://example.test/aaa.jpg", "proj", 0)
        second = pipeline.download_media_asset("https://example.test/bbb.jpg", "proj", 0)
        assert first is not None and second is not None
        assert first != second, "different URLs must not collide on one cache file"
        assert first.read_bytes() != second.read_bytes(), "each URL must keep its own payload"

    def test_the_same_url_is_served_from_cache(self, tmp_path, monkeypatch):
        import backend.services.pipeline as pipeline

        monkeypatch.setattr(pipeline, "STORAGE_DIR", tmp_path)
        calls = []

        class FakeResponse:
            content = b"\xff\xd8\xff" + b"b" * 2048

            def raise_for_status(self):
                return None

        def fake_get(url, **kwargs):
            calls.append(url)
            return FakeResponse()

        monkeypatch.setattr(pipeline.httpx, "get", fake_get)

        first = pipeline.download_media_asset("https://example.test/a.jpg", "proj", 0)
        second = pipeline.download_media_asset("https://example.test/a.jpg", "proj", 0)
        assert first == second
        assert len(calls) == 1, "an unchanged URL must not be downloaded twice"

    def test_a_local_path_is_copied_without_a_network_call(self, tmp_path, monkeypatch):
        import backend.services.pipeline as pipeline

        source = tmp_path / "local.jpg"
        source.write_bytes(b"\xff\xd8\xff" + b"c" * 2048)
        monkeypatch.setattr(pipeline, "STORAGE_DIR", tmp_path)

        def boom(*args, **kwargs):
            raise AssertionError("a local path must not hit the network")

        monkeypatch.setattr(pipeline.httpx, "get", boom)
        result = pipeline.download_media_asset(str(source), "proj", 0)
        assert result is not None and result.exists()
        assert result.read_bytes() == source.read_bytes()

    def test_a_tiny_payload_is_rejected(self, tmp_path, monkeypatch):
        import backend.services.pipeline as pipeline

        monkeypatch.setattr(pipeline, "STORAGE_DIR", tmp_path)

        class FakeResponse:
            content = b"<html>404</html>"

            def raise_for_status(self):
                return None

        monkeypatch.setattr(pipeline.httpx, "get", lambda url, **kw: FakeResponse())
        assert pipeline.download_media_asset("https://example.test/x.jpg", "proj", 0) is None


class TestEscapeHtml:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("<b>", "&lt;b&gt;"),
            ("a & b", "a &amp; b"),
            ('say "hi"', "say &quot;hi&quot;"),
            ("it's", "it&#x27;s"),
        ],
    )
    def test_escapes_markup_characters(self, monkeypatch, raw, expected):
        assert escape_html(raw) == expected

    def test_escaping_is_not_an_identity_replacement(self):
        assert escape_html("<script>") != "<script>"


class TestRemovedLyingPaths:
    def test_pipeline_no_longer_exports_a_fake_render(self):
        import backend.services.pipeline as pipeline

        assert not hasattr(pipeline, "create_render_summary"), (
            "create_render_summary reported status=rendered without rendering anything"
        )
        assert not hasattr(pipeline, "render_video_from_srt"), (
            "the superseded FFmpeg renderer should stay deleted"
        )


class TestKeywordExtraction:
    def test_keywords_are_unique_and_bounded(self):
        text = "corrida corrida corrida intensidade intensidade saude saude mente"
        keywords = extract_keywords_from_text(text)
        assert len(keywords) == len(set(k.lower() for k in keywords))
        assert len(keywords) <= 8

    def test_short_words_are_dropped(self):
        assert extract_keywords_from_text("a de para com") == []

    def test_real_srt_yields_keywords(self):
        from backend.services.pipeline import UPLOAD_DIR

        srt = UPLOAD_DIR / "api_test3.srt"
        if not srt.exists():
            pytest.skip("fixture srt not present")
        assert extract_keywords_from_text(srt.read_text(encoding="utf-8"))


class TestSrtRoundTrip:
    def test_segments_parse_back_to_their_timings(self):
        rows = [(0.0, 2.5, "primeira"), (2.5, 7.25, "segunda")]
        storyboard = build_storyboard_from_segments(_segments(rows))
        assert storyboard[0]["start"] == pytest.approx(0.0)
        # Later scenes start early by the transition overlap; the source time is
        # preserved on source_start for audit.
        assert storyboard[1]["source_start"] == pytest.approx(2.5)
        assert storyboard[1]["start"] == pytest.approx(2.5 - __import__(
            "backend.services.pipeline", fromlist=["SCENE_OVERLAP"]
        ).SCENE_OVERLAP)

    def test_missing_srt_returns_empty(self):
        from backend.services.pipeline import UPLOAD_DIR

        assert parse_srt_to_segments(UPLOAD_DIR / "definitely_absent.srt") == []

# --------------------------------------------------------------------------
# Wave 1: user-configurable subtitle styling, theme presets and background
# music. The schema modules (style.py, music.py) own validation; these tests
# cover the renderer's side of the contract: that the style actually reaches the
# CSS, that the preset never mutates the caller's storyboard, and that the music
# stage can fail without ever losing a render.
# --------------------------------------------------------------------------

import asyncio  # noqa: E402  (kept next to the tests that use them)
import os  # noqa: E402
import pathlib  # noqa: E402
import signal  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402

from backend.services.style import SubtitleStyle, get_preset, preset_names  # noqa: E402
from backend.services.render_engine import (  # noqa: E402
    apply_preset_to_storyboard,
    build_subtitle_css,
    mix_audio_track,
    render_video_hyperframes,
)

# Values a settings form must never be able to smuggle past the CSS block.
HOSTILE_STYLE = {
    "font_family": "Inter; }body{display:none",
    "primary_color": "red; }body{display:none",
    "stroke_color": "#fff",
    "background_color": "url(https://evil.test/x.svg)",
}


def _storyboard(rows=None):
    """Two plain scenes with no background of their own.

    ``build_storyboard_from_segments`` paints every scene its own background and
    picks a caption style per scene, which would mask the preset palette and the
    configured mode. These scenes say nothing, so the preset and the style are
    the only things that can decide.
    """
    scenes = [
        {"index": 1, "start": 0.0, "end": 4.0, "duration": 4.0, "caption": "primeira"},
        {"index": 2, "start": 4.0, "end": 8.0, "duration": 4.0, "caption": "segunda"},
    ]
    for scene, (start, end, text) in zip(scenes, rows or []):
        scene.update({"start": start, "end": end, "duration": end - start, "caption": text})
    return scenes


def _karaoke_storyboard():
    storyboard = _storyboard()
    storyboard[0]["words"] = [
        {"word": "primeira", "start": 0.2, "end": 0.9},
        {"word": "palavra", "start": 0.9, "end": 1.6},
        {"word": "final", "start": 1.6, "end": 2.4},
    ]
    storyboard[0]["caption_style"] = "karaoke"
    return storyboard


def _css_block(html):
    return html.split("<style>", 1)[1].split("</style>", 1)[0]


class TestSubtitleStyleReachesTheCss:
    def test_font_size_colour_and_stroke_appear_in_the_generated_css(self):
        style = {
            "font_size": 96,
            "primary_color": "#22D3EE",
            "stroke_color": "#0E7490",
            "stroke_width": 7,
            "font_family": "Montserrat",
        }
        html = generate_composition_html(
            "p", _storyboard(), {}, None, "vertical", 8.0, subtitle_style=style
        )
        css = _css_block(html)
        assert "font-size: 96px" in css
        assert "color: #22D3EE" in css
        assert "-webkit-text-stroke: 7px #0E7490" in css
        assert "font-family: Montserrat" in css

    def test_out_of_range_numbers_are_clamped_not_trusted(self):
        html = generate_composition_html(
            "p", _storyboard(), {}, None, "vertical", 8.0,
            subtitle_style={"font_size": 9999, "stroke_width": -5},
        )
        css = _css_block(html)
        assert "font-size: 160px" in css, "font size must clamp to FONT_SIZE_MAX"
        assert "-webkit-text-stroke: 0px #000000" in css, "stroke clamps to 0, never negative"

    def test_a_hijacked_style_cannot_escape_the_css_block(self):
        html = generate_composition_html(
            "p", _storyboard(), {}, None, "vertical", 8.0, subtitle_style=HOSTILE_STYLE
        )
        css = _css_block(html)
        assert "display:none" not in css
        assert "url(" not in css
        assert "javascript:" not in css
        # The bad values fell back to their defaults rather than being emitted.
        assert "font-family: Inter" in css
        assert "color: #FFFFFF" in css
        # Braces stay balanced: the injected "}" cannot close the rule early.
        assert css.count("{") == css.count("}")
        assert css.count("{") >= 2

    def test_position_moves_the_container_alignment(self):
        top = generate_composition_html(
            "p", _storyboard(), {}, None, "vertical", 8.0,
            subtitle_style={"position": "top"},
        )
        bottom = generate_composition_html(
            "p", _storyboard(), {}, None, "vertical", 8.0,
            subtitle_style={"position": "bottom"},
        )
        assert "align-items: flex-start" in _css_block(top)
        assert "align-items: flex-end" in _css_block(bottom)
        assert "align-items: flex-start" not in _css_block(bottom)

    def test_the_styled_block_outranks_the_hardcoded_variants(self):
        """The hook variant is two classes deep, so a bare rule would lose."""
        html = generate_composition_html(
            "p", _storyboard(), {}, None, "vertical", 8.0,
            subtitle_style={"mode": "hook", "font_size": 120},
        )
        styled = build_subtitle_css(SubtitleStyle.from_dict({"mode": "hook", "font_size": 120}))
        assert ".caption.hook .caption-text" in styled, (
            "the styled rule must match the variant rule's specificity"
        )
        assert html.index(styled) > html.index(".caption.hook .caption-text"), (
            "the styled block must come after the static variants to win"
        )

    def test_build_subtitle_css_uses_the_style_and_the_position_table(self):
        css = build_subtitle_css(
            SubtitleStyle.from_dict({"position": "center", "font_size": 40})
        )
        assert css.count("{") == css.count("}") == 2
        assert "align-items: center" in css
        assert "font-size: 40px" in css


class TestPresetApplication:
    def test_a_preset_paints_palette_transition_and_effect(self):
        storyboard = _storyboard()
        preset = get_preset("neon")
        styled = apply_preset_to_storyboard(storyboard, "neon")

        backgrounds = [scene.get("background") for scene in styled]
        assert backgrounds == [preset.palette[0], preset.palette[1]]
        assert all(scene["transition"] == preset.transition for scene in styled)
        assert all(scene["effect"] == preset.effect for scene in styled)

    def test_an_explicit_background_is_never_overwritten(self):
        storyboard = _storyboard()
        storyboard[0]["background"] = "#123456"
        styled = apply_preset_to_storyboard(storyboard, "neon")
        assert styled[0]["background"] == "#123456"

    def test_the_input_storyboard_is_never_mutated(self):
        storyboard = _storyboard()
        snapshot = [dict(scene) for scene in storyboard]
        styled = apply_preset_to_storyboard(storyboard, "bold")
        assert storyboard == snapshot
        assert styled is not storyboard
        assert all(copy is not scene for copy, scene in zip(styled, storyboard))

    def test_no_preset_returns_equal_copies(self):
        storyboard = _storyboard()
        snapshot = [dict(scene) for scene in storyboard]
        styled = apply_preset_to_storyboard(storyboard, None)
        assert styled == snapshot
        assert styled[0] is not storyboard[0]

    def test_an_unknown_preset_name_falls_back_without_raising(self):
        styled = apply_preset_to_storyboard(_storyboard(), "no-such-preset")
        assert len(styled) == 2

    def test_every_catalogue_preset_renders(self):
        for name in preset_names():
            html = generate_composition_html(
                "p", _storyboard(), {}, None, "vertical", 8.0, preset=name
            )
            assert "caption-text" in html
            assert html.count("{") == html.count("}")

    def test_preset_reaches_the_scene_backgrounds_in_the_html(self):
        palette = get_preset("podcast").palette
        html = generate_composition_html(
            "p", _storyboard(), {}, None, "vertical", 8.0, preset="podcast"
        )
        assert f'background:{palette[0]};' in html
        assert f'background:{palette[1]};' in html


class TestStyledComposition:
    def test_default_render_carries_no_styled_block_and_keeps_the_hook(self):
        storyboard = _storyboard()
        for scene in storyboard:
            scene["caption_style"] = "bottom"
        html = generate_composition_html(
            "p", storyboard, {}, None, "vertical", 8.0
        )
        assert "Configurable caption styling" not in html
        assert 'class="clip caption hook"' in html, (
            "unconfigured renders keep the legacy hook opening frame"
        )
        assert 'class="clip caption bottom"' in html

    def test_karaoke_mode_with_a_style_still_emits_word_spans_and_tweens(self):
        storyboard = _karaoke_storyboard()
        words = storyboard[0]["words"]
        html = generate_composition_html(
            "p", storyboard, {}, None, "vertical", 8.0,
            subtitle_style={"mode": "karaoke", "font_size": 64, "position": "center"},
        )
        assert html.count('class="karaoke-word"') == 3
        assert '<span class="karaoke-word">primeira</span> <span class="karaoke-word">' in html, (
            "inline-block words must stay separated by spaces"
        )
        for idx in range(1, len(words) + 1):
            assert f"#caption-0 .karaoke-word:nth-of-type({idx})" in html
        assert "align-items: center" in _css_block(html)
        assert "font-size: 64px" in _css_block(html)

    def test_a_scenes_own_caption_style_overrides_the_global_mode(self):
        storyboard = _storyboard()
        storyboard[0]["caption_style"] = "bottom"
        storyboard[1]["caption_style"] = "center"
        html = generate_composition_html(
            "p", storyboard, {}, None, "vertical", 8.0,
            subtitle_style={"mode": "hook", "font_size": 90},
        )
        # The global mode supplies the typography to both...
        assert "font-size: 90px" in _css_block(html)
        # ...but the per-scene override decides each caption's variant.
        assert 'class="clip caption bottom"' in html
        assert 'class="clip caption center"' in html
        assert 'class="clip caption hook"' not in html, (
            "a scene's explicit caption_style must beat the configured mode"
        )

    def test_the_configured_mode_applies_when_a_scene_says_nothing(self):
        html = generate_composition_html(
            "p", _storyboard(), {}, None, "vertical", 8.0,
            subtitle_style={"mode": "center"},
        )
        assert 'class="clip caption center"' in html

    def test_hidden_mode_still_suppresses_the_caption(self):
        storyboard = _storyboard()
        storyboard[0]["caption_style"] = "hidden"
        html = generate_composition_html(
            "p", storyboard, {}, None, "vertical", 8.0,
            subtitle_style={"mode": "karaoke"},
        )
        assert "caption-0" not in html

    def test_styling_does_not_break_the_renderer_contract(self):
        html = generate_composition_html(
            "p", _storyboard(), {0: "assets/a.png"}, None, "vertical", 8.0,
            subtitle_style={"mode": "karaoke"},
        )
        assert 'data-composition-id="main"' in html
        assert "gsap.timeline({ paused: true })" in html
        assert 'window.__timelines["main"] = tl' in html
        assert "repeat: -1" not in html
        assert 'src="assets/a.png"' in html


class TestMixAudioTrack:
    def _files(self, tmp_path):
        video = tmp_path / "video.mp4"
        video.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"0" * 128)
        track = tmp_path / "bed.wav"
        track.write_bytes(b"RIFF" + b"0" * 128)
        return video, track

    def test_a_missing_ffmpeg_returns_the_original_and_never_raises(
        self, monkeypatch, tmp_path
    ):
        video, track = self._files(tmp_path)
        output = tmp_path / "mixed.mp4"
        monkeypatch.setattr(
            render_engine_module(), "subprocess",
            _raise_missing_binary(),
        )
        result = mix_audio_track(video, track, output, total_duration=8.0)
        assert result == video, "a failed mix must return the untouched original"
        assert not output.exists(), "no partial file may be left at the output path"
        assert video.exists()

    def test_a_successful_mix_writes_to_the_output_path(self, monkeypatch, tmp_path):
        video, track = self._files(tmp_path)
        output = tmp_path / "mixed.mp4"
        calls = []
        monkeypatch.setattr(render_engine_module(), "subprocess", _fake_ffmpeg(calls))
        result = mix_audio_track(
            video, track, output, music_volume=0.25, total_duration=8.0
        )
        assert result == output
        assert output.exists() and output.stat().st_size > 0
        # calls holds one argv list per invocation, so call[0] IS the
        # program name; call[0][0] would be only its first character.
        ffmpeg_calls = [call for call in calls if call[0] == "ffmpeg"]
        assert ffmpeg_calls, "the mix must actually shell out to ffmpeg"
        command = ffmpeg_calls[0]
        assert "-filter_complex" in command
        assert "sidechaincompress" in "".join(command), "ducking uses the voice as key"
        assert "-c:v" in command and command[command.index("-c:v") + 1] == "copy", (
            "video is stream-copied, never re-encoded"
        )
        graph = command[command.index("-filter_complex") + 1]
        assert "volume=0.2500" in graph, "the requested music volume must reach ffmpeg"

    def test_a_silent_video_gets_the_music_as_its_only_audio(self, monkeypatch, tmp_path):
        video, track = self._files(tmp_path)
        output = tmp_path / "mixed.mp4"
        calls = []
        monkeypatch.setattr(
            render_engine_module(), "subprocess",
            _fake_ffmpeg(calls, audio_streams=()),
        )
        result = mix_audio_track(video, track, output, total_duration=8.0)
        assert result == output
        command = [call for call in calls if call[0] == "ffmpeg"][0]
        assert "-filter_complex" not in command
        # The video comes from input 0 and the music from input 1, so the
        # SECOND -map (not the first) is the stream that becomes the
        # audio: index() alone would land on the video map.
        maps = [command[i + 1] for i, arg in enumerate(command) if arg == "-map"]
        assert maps == ["0:v:0", "1:a:0"], (
            "a silent video keeps input 0's video and takes the bed from input 1"
        )

    def test_without_a_music_track_the_original_is_returned(self, tmp_path):
        video, track = self._files(tmp_path)
        assert mix_audio_track(video, None, tmp_path / "out.mp4") == video

    def test_no_ducking_still_consumes_every_filter_label(self, monkeypatch, tmp_path):
        """A duck_voice=False mix must not leave a filter output dangling.

        ffmpeg treats any labelled filter output that no other filter and no
        -map consumes as fatal ("Filter asplit has an unconnected output"), and
        that abort used to hide behind the mixer's catch-all: the render still
        succeeded, just silently without the bed.
        """
        video, track = self._files(tmp_path)
        output = tmp_path / "mixed.mp4"
        calls = []
        monkeypatch.setattr(render_engine_module(), "subprocess", _fake_ffmpeg(calls))
        result = mix_audio_track(
            video, track, output, duck_voice=False, total_duration=8.0
        )
        assert result == output
        command = [call for call in calls if call[0] == "ffmpeg"][0]
        graph = command[command.index("-filter_complex") + 1]
        assert "sidechaincompress" not in graph, "ducking is off for this call"
        declared, consumed = _graph_labels(graph)
        # A graph output may be claimed by -map rather than by a later filter.
        mapped = [
            arg[1:-1] for arg in command
            if arg.startswith("[") and arg.endswith("]") and arg[1].isalpha()
        ]
        assert declared[-1] == "out", "the graph's final output is [out]"
        assert "out" in mapped, "and -map carries it into the file"
        # Every labelled output has to be picked up by a later filter or by
        # -map; ffmpeg exits non-zero on the leftovers rather than ignoring them.
        assert sorted(consumed + mapped) == sorted(declared), (
            "ffmpeg aborts on a filter output nothing consumes"
        )

    def test_a_zero_volume_is_a_no_op(self, monkeypatch, tmp_path):
        video, track = self._files(tmp_path)
        monkeypatch.setattr(
            render_engine_module(), "subprocess",
            _raise_missing_binary(),
        )
        result = mix_audio_track(
            video, track, tmp_path / "out.mp4", music_volume=0.0, total_duration=8.0
        )
        assert result == video


def render_engine_module():
    import backend.services.render_engine as engine

    return engine


def _raise_missing_binary():
    class Missing:
        @staticmethod
        def run(*args, **kwargs):
            raise FileNotFoundError("ffmpeg")

    return Missing


def _fake_ffmpeg(calls, audio_streams=("audio",)):
    class FakeCompleted:
        def __init__(self, stdout):
            self.returncode = 0
            self.stdout = stdout
            self.stderr = ""

    class Fake:
        @staticmethod
        def run(command, **kwargs):
            calls.append(list(command))
            if command[0] == "ffprobe":
                return FakeCompleted("\n".join(audio_streams))
            # ffmpeg was asked to write its last argument: create it, so the
            # mixer sees the staged file and moves it into place.
            Path = __import__("pathlib").Path
            Path(command[-1]).write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"0" * 256)
            return FakeCompleted("")

    return Fake


def _graph_labels(graph):
    """Split a filter_complex graph into its declared and consumed labels.

    A stream selector ("[0:a]") is not a label; only named pads count.
    """
    declared, consumed = [], []
    for chain in graph.split(";"):
        chain = chain.strip()
        if not chain:
            continue
        match = re.search(r'\[([A-Za-z_][A-Za-z0-9_]*)\]$', chain)
        head = chain[: chain.rindex('[')] if match else chain
        consumed.extend(re.findall(r'\[([A-Za-z_][A-Za-z0-9_]*)\]', head))
        if match:
            declared.append(match.group(1))
    return declared, consumed


class TestRenderPayloadCarriesTheNewKeys:
    def _patched(self, monkeypatch, tmp_path):
        engine = render_engine_module()
        monkeypatch.setattr(engine, "OUTPUT_DIR", tmp_path)
        monkeypatch.setattr(engine, "stage_project_assets", lambda *a, **k: ({}, None, []))
        monkeypatch.setattr(engine, "get_media_duration", lambda _p: 5.0)
        monkeypatch.setattr(engine, "cleanup_render_dirs", lambda keep=None: 0)

        async def fake_render(project_name, composition_html, output_path, *args):
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"0" * 128)
            return {"status": "rendered", "output_path": str(output_path), "stdout": ""}

        monkeypatch.setattr(engine, "render_with_hyperframes", fake_render)
        return engine

    def test_a_plain_render_reports_the_resolved_look_and_no_music(self, monkeypatch, tmp_path):
        engine = self._patched(monkeypatch, tmp_path)
        srt = tmp_path / "proj.srt"
        srt.write_text("1\n00:00:00,000 --> 00:00:04,000\nfalou\n", encoding="utf-8")
        result = asyncio.run(engine.render_video_hyperframes("proj", srt))
        assert result["status"] == "rendered"
        assert result["preset"] == "cinematic"
        assert result["subtitle_style"]["font_size"] == 52
        reported = result["music"]
        assert sorted(reported) == sorted(
            "requested applied track_id volume duck_voice note".split()
        )
        assert reported["requested"] is None
        assert reported["applied"] is False
        assert reported["track_id"] is None
        assert reported["volume"] == 0.18
        assert reported["duck_voice"] is True
        # A video with no bed is a result, not a failure the user can guess at:
        # the note must say so, in Portuguese, reusing the wording that lives
        # with the library that failed to appear. Only the note changed; the
        # other fields above stay exactly what they were.
        assert reported["note"] == missing_library_note()
        assert reported["note"].strip(), (
            "a silent video with an empty note is the defect this pins"
        )

    def test_a_configured_style_and_preset_are_echoed_back(self, monkeypatch, tmp_path):
        engine = self._patched(monkeypatch, tmp_path)
        srt = tmp_path / "proj2.srt"
        srt.write_text("1\n00:00:00,000 --> 00:00:04,000\nfalou\n", encoding="utf-8")
        result = asyncio.run(
            engine.render_video_hyperframes(
                "proj2", srt, preset="neon",
                subtitle_style={"font_size": 33, "position": "top"},
            )
        )
        assert result["preset"] == "neon"
        assert result["subtitle_style"]["font_size"] == 33
        assert result["subtitle_style"]["position"] == "top"
        # The preset's own caption look survives an override on unrelated fields.
        assert result["subtitle_style"]["font_family"] == "Bebas Neue"

    def test_an_unknown_music_track_still_renders(self, monkeypatch, tmp_path):
        engine = self._patched(monkeypatch, tmp_path)
        srt = tmp_path / "proj3.srt"
        srt.write_text("1\n00:00:00,000 --> 00:00:04,000\nfalou\n", encoding="utf-8")
        result = asyncio.run(
            engine.render_video_hyperframes("proj3", srt, music_track="no-such-track")
        )
        assert result["status"] == "rendered", "an unknown track must not fail the render"
        assert result["output_path"]
        assert result["music"]["requested"] == "no-such-track"
        assert result["music"]["applied"] is False
        assert result["music"]["track_id"] is None
        assert "no-such-track" in result["music"]["note"]

    def test_the_error_path_still_carries_the_new_keys(self, monkeypatch, tmp_path):
        engine = self._patched(monkeypatch, tmp_path)

        async def boom(*args, **kwargs):
            raise RuntimeError("npx exploded")

        monkeypatch.setattr(engine, "render_with_hyperframes", boom)
        srt = tmp_path / "proj4.srt"
        srt.write_text("1\n00:00:00,000 --> 00:00:04,000\nfalou\n", encoding="utf-8")
        result = asyncio.run(
            engine.render_video_hyperframes(
                "proj4", srt, preset="bold", music_track="whatever", music_volume=0.3
            )
        )
        assert result["status"] == "error"
        assert result["preset"] == "bold"
        assert isinstance(result["subtitle_style"], dict)
        assert result["music"]["applied"] is False
        assert result["music"]["volume"] == 0.3
        assert result["music"]["note"]


class TestBackgroundMusicReportIsNeverSilent:
    """``music.note`` is the only channel the job result has.

    A render that ends with no bed used to report ``applied: False`` with an
    empty note, which told the user nothing at all. The other branch has to stay
    honest in the opposite direction: a bed that was mixed is a success and must
    not be dressed up as the failure above.
    """

    def _engine(self, monkeypatch, tmp_path):
        engine = render_engine_module()
        monkeypatch.setattr(engine, "OUTPUT_DIR", tmp_path)
        return engine

    def test_no_track_at_all_is_explained_in_portuguese(self, monkeypatch, tmp_path):
        engine = self._engine(monkeypatch, tmp_path)
        video = tmp_path / "sem-musica.mp4"
        video.write_bytes(MP4_BYTES)

        path, reported = engine._apply_background_music(video, "", 0.18, True, 5.0)

        assert path == video, "a missing bed must not cost the render"
        assert reported["requested"] is None
        assert reported["applied"] is False, "nothing was mixed, so nothing applied"
        assert reported["track_id"] is None
        note = reported["note"]
        assert note == missing_library_note(), (
            "one phrasing of this failure, owned by the music library"
        )
        assert note.strip()
        for word in ("musica", "trilha", "video"):
            assert word in note, note

    def test_an_applied_track_reports_the_mix_not_the_failure_note(self, monkeypatch, tmp_path):
        engine = self._engine(monkeypatch, tmp_path)
        video = tmp_path / "com-musica.mp4"
        video.write_bytes(MP4_BYTES)
        source = tmp_path / "ambient-drift.wav"
        source.write_bytes(MP4_BYTES)

        class FakeTrack:
            id = "ambient-drift"
            path = str(source)

        monkeypatch.setattr(engine.music, "get_track", lambda track_id: FakeTrack())
        monkeypatch.setattr(
            engine.music,
            "loop_to_duration",
            lambda track, duration, output: pathlib.Path(output),
        )

        def fake_mix(video_in, track_in, output, **kwargs):
            pathlib.Path(output).write_bytes(MP4_BYTES)
            return pathlib.Path(output)

        monkeypatch.setattr(engine, "mix_audio_track", fake_mix)

        path, reported = engine._apply_background_music(
            video, "ambient-drift", 0.18, True, 5.0
        )

        assert path == video
        assert reported["applied"] is True
        assert reported["track_id"] == "ambient-drift"
        assert "misturada" in reported["note"]
        assert reported["note"] != missing_library_note(), (
            "a mixed bed must not be reported as a missing library"
        )


class TestRenderWatchdogIsBounded:
    """A render must not be able to pin the server process forever.

    The subprocess used to be awaited with a bare ``await proc.communicate()``:
    no bound, no cancellation handling, nothing able to stop chrome once the
    HTTP client had given up. These tests fake the subprocess - no HyperFrames,
    no multi-minute render - and assert the properties the fix depends on: the
    wait is bounded, the whole tree is killed, a cancellation propagates, and a
    timeout degrades into the ordinary Portuguese failure payload.
    """

    def _engine(self, monkeypatch, tmp_path):
        engine = render_engine_module()
        monkeypatch.setattr(engine, "OUTPUT_DIR", tmp_path)
        return engine

    def _stub_exec(self, monkeypatch, engine, proc):
        """Swap the spawn call so no npx or chrome is ever launched."""
        calls = []

        async def fake_exec(*args, **kwargs):
            calls.append((list(args), kwargs))
            return proc

        monkeypatch.setattr(engine.asyncio, "create_subprocess_exec", fake_exec)
        return calls

    def _kill_recorder(self, monkeypatch, engine):
        """Replace the tree killer and record what it was asked to kill."""
        killed = []

        async def fake_kill(proc, **kwargs):
            killed.append((proc, kwargs))

        monkeypatch.setattr(engine, "_kill_process_tree", fake_kill)
        return killed

    def _render(self, engine, tmp_path, stem, proc):
        output = tmp_path / f"{stem}.mp4"
        return engine.render_with_hyperframes(
            stem, "<html></html>", output, tmp_path, 1080, 1920
        )

    # ----------------------------------------------------------------- happy path

    def test_a_render_that_finishes_in_time_returns_the_same_dict(
        self, monkeypatch, tmp_path
    ):
        engine = self._engine(monkeypatch, tmp_path)
        output = tmp_path / "proj.mp4"
        proc = _FakeProc(stdout=b"rendered 41s", returncode=0)
        self._stub_exec(monkeypatch, engine, proc)
        output.write_bytes(b"\x00\x00\x00\x18ftypmp42")

        result = asyncio.run(self._render(engine, tmp_path, "proj", proc))

        assert result == {
            "status": "rendered",
            "output_path": str(output),
            "stdout": "rendered 41s",
        }, "the success payload must be unchanged by the watchdog"

    def test_a_render_that_exits_cleanly_is_never_signalled(
        self, monkeypatch, tmp_path
    ):
        """Paying the tree-kill cost on every successful render is not the fix."""
        engine = self._engine(monkeypatch, tmp_path)
        proc = _FakeProc(stdout=b"", returncode=0)
        self._stub_exec(monkeypatch, engine, proc)
        killed = self._kill_recorder(monkeypatch, engine)
        (tmp_path / "proj.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42")

        asyncio.run(self._render(engine, tmp_path, "proj", proc))

        assert killed == []
        assert proc.signals == []
        assert not proc.terminated and not proc.killed

    def test_the_render_is_spawned_in_its_own_session_on_posix(
        self, monkeypatch, tmp_path
    ):
        """start_new_session is what makes the whole tree killable later on."""
        engine = self._engine(monkeypatch, tmp_path)
        monkeypatch.setattr(engine, "_is_windows", lambda: False)
        proc = _FakeProc(stdout=b"", returncode=0)
        calls = self._stub_exec(monkeypatch, engine, proc)
        (tmp_path / "proj.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42")

        asyncio.run(self._render(engine, tmp_path, "proj", proc))

        _argv, kwargs = calls[0]
        assert kwargs.get("start_new_session") is True, (
            "without its own session npx leads no group and killpg cannot reach chrome"
        )

    def test_start_new_session_is_never_passed_on_windows(self, monkeypatch, tmp_path):
        """Windows rejects the kwarg outright, so the branch must not reach it."""
        engine = self._engine(monkeypatch, tmp_path)
        monkeypatch.setattr(engine, "_is_windows", lambda: True)
        proc = _FakeProc(stdout=b"", returncode=0)
        calls = self._stub_exec(monkeypatch, engine, proc)
        (tmp_path / "proj.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42")

        asyncio.run(self._render(engine, tmp_path, "proj", proc))

        _argv, kwargs = calls[0]
        assert "start_new_session" not in kwargs

    # ------------------------------------------------------------------- timeout

    def test_a_render_over_the_timeout_never_reports_success(
        self, monkeypatch, tmp_path
    ):
        """The user gets the ordinary Portuguese failure, not a traceback."""
        engine = self._engine(monkeypatch, tmp_path)
        monkeypatch.setattr(engine, "stage_project_assets", lambda *a, **k: ({}, None, []))
        monkeypatch.setattr(engine, "get_media_duration", lambda _p: 5.0)
        monkeypatch.setattr(engine, "cleanup_render_dirs", lambda keep=None: 0)
        monkeypatch.setenv("DARK_STUDIO_RENDER_TIMEOUT", "0.05")
        proc = _FakeProc(returncode=None, hang=True)
        self._stub_exec(monkeypatch, engine, proc)
        killed = self._kill_recorder(monkeypatch, engine)
        srt = tmp_path / "proj5.srt"
        srt.write_text("1\n00:00:00,000 --> 00:00:04,000\nfalou\n", encoding="utf-8")

        result = asyncio.run(engine.render_video_hyperframes("proj5", srt))

        assert result["status"] == "error", "a killed render must never read as rendered"
        assert "tempo limite de render excedido" in result["message"], (
            f"a timeout must read like any other render failure, got: {result['message']}"
        )
        assert "Falha no render HyperFrames" in result["message"]
        # The error payload keeps the contract every caller relies on.
        assert result["music"]["applied"] is False
        assert isinstance(result["subtitle_style"], dict)
        assert len(killed) == 1 and killed[0][0] is proc, (
            "the tree killer must be called once, with the render process itself"
        )

    def test_a_timed_out_render_leaves_no_half_written_mp4(
        self, monkeypatch, tmp_path
    ):
        """A truncated MP4 at the output path looks like a finished deliverable."""
        engine = self._engine(monkeypatch, tmp_path)
        monkeypatch.setenv("DARK_STUDIO_RENDER_TIMEOUT", "0.05")
        output = tmp_path / "proj6.mp4"
        proc = _FakeProc(returncode=None, hang=True)
        proc.writes_output = output
        self._stub_exec(monkeypatch, engine, proc)
        self._kill_recorder(monkeypatch, engine)

        with pytest.raises(RuntimeError):
            asyncio.run(self._render(engine, tmp_path, "proj6", proc))

        assert not output.exists(), "the aborted render must not advertise an MP4"

    def test_a_previous_render_is_never_deleted_by_a_later_timeout(
        self, monkeypatch, tmp_path
    ):
        """Every render of a project shares this exact output path."""
        engine = self._engine(monkeypatch, tmp_path)
        monkeypatch.setenv("DARK_STUDIO_RENDER_TIMEOUT", "0.05")
        output = tmp_path / "proj7.mp4"
        output.write_bytes(b"\x00\x00\x00\x18ftypmp42goodvideo")
        proc = _FakeProc(returncode=None, hang=True)
        proc.writes_output = output
        self._stub_exec(monkeypatch, engine, proc)
        self._kill_recorder(monkeypatch, engine)

        with pytest.raises(RuntimeError):
            asyncio.run(self._render(engine, tmp_path, "proj7", proc))

        assert output.exists(), "losing a finished video is worse than the timeout"

    # -------------------------------------------------------------- cancellation

    def test_cancellation_kills_the_tree_and_propagates(self, monkeypatch, tmp_path):
        """CancelledError is a BaseException: it must not be absorbed."""
        engine = self._engine(monkeypatch, tmp_path)
        monkeypatch.setenv("DARK_STUDIO_RENDER_TIMEOUT", "600")
        proc = _FakeProc(returncode=None, hang=True)
        self._stub_exec(monkeypatch, engine, proc)
        killed = self._kill_recorder(monkeypatch, engine)

        async def main():
            task = asyncio.ensure_future(self._render(engine, tmp_path, "proj8", proc))
            await asyncio.sleep(0.05)  # let the spawn and the wait start
            task.cancel()
            return await task

        with pytest.raises(asyncio.CancelledError):
            asyncio.run(main())

        assert len(killed) == 1 and killed[0][0] is proc, (
            "cancellation must kill the render tree before propagating"
        )
        assert not (tmp_path / "proj8.mp4").exists()

    def test_a_cancelled_render_is_not_turned_into_a_payload(self, monkeypatch, tmp_path):
        """render_video_hyperframes must not answer a cancelled render at all."""
        engine = self._engine(monkeypatch, tmp_path)
        monkeypatch.setattr(engine, "stage_project_assets", lambda *a, **k: ({}, None, []))
        monkeypatch.setattr(engine, "get_media_duration", lambda _p: 5.0)
        monkeypatch.setattr(engine, "cleanup_render_dirs", lambda keep=None: 0)
        srt = tmp_path / "proj9.srt"
        srt.write_text("1\n00:00:00,000 --> 00:00:04,000\nfalou\n", encoding="utf-8")

        async def cancelled(*args, **kwargs):
            raise asyncio.CancelledError()

        monkeypatch.setattr(engine, "render_with_hyperframes", cancelled)

        with pytest.raises(asyncio.CancelledError):
            asyncio.run(engine.render_video_hyperframes("proj9", srt))

        assert not (tmp_path / "proj9.json").exists(), (
            "a cancelled render must not leave a summary that reads as finished"
        )

    # -------------------------------------------------------------- the bound

    def test_the_bound_is_read_from_the_environment_at_call_time(self, monkeypatch):
        """A value frozen at import time is what made the watchdog untunable."""
        engine = render_engine_module()
        monkeypatch.delenv("DARK_STUDIO_RENDER_TIMEOUT", raising=False)
        assert engine.render_timeout_seconds() == engine.DEFAULT_RENDER_TIMEOUT

        monkeypatch.setenv("DARK_STUDIO_RENDER_TIMEOUT", "42")
        assert engine.render_timeout_seconds() == 42.0

        monkeypatch.setenv("DARK_STUDIO_RENDER_TIMEOUT", "1200")
        assert engine.render_timeout_seconds() == 1200.0

    def test_the_default_clears_a_real_slow_render(self):
        """~316s was measured for a 41s 1080x1920 cut on a 2-vCPU box."""
        engine = render_engine_module()
        assert engine.DEFAULT_RENDER_TIMEOUT >= 900.0, (
            "a watchdog tighter than the measured render kills valid work"
        )

    @pytest.mark.parametrize("raw", ["", "   ", "abc", "0", "-5"])
    def test_a_nonsense_timeout_falls_back_to_the_default(self, monkeypatch, raw):
        engine = render_engine_module()
        monkeypatch.setenv("DARK_STUDIO_RENDER_TIMEOUT", raw)
        assert engine.render_timeout_seconds() == engine.DEFAULT_RENDER_TIMEOUT

    def test_the_environment_value_is_the_bound_the_wait_uses(
        self, monkeypatch, tmp_path
    ):
        """Not just the helper: the value has to reach asyncio.wait_for."""
        engine = self._engine(monkeypatch, tmp_path)
        monkeypatch.setenv("DARK_STUDIO_RENDER_TIMEOUT", "0.05")
        proc = _FakeProc(returncode=None, hang=True)
        self._stub_exec(monkeypatch, engine, proc)

        bounds = []
        real_wait_for = engine.asyncio.wait_for

        def recording_wait_for(awaitable, timeout, *args, **kwargs):
            bounds.append(timeout)
            return real_wait_for(awaitable, timeout, *args, **kwargs)

        monkeypatch.setattr(engine.asyncio, "wait_for", recording_wait_for)

        with pytest.raises(RuntimeError):
            asyncio.run(self._render(engine, tmp_path, "proj11", proc))

        assert bounds and bounds[0] == 0.05, (
            f"the render wait must be bounded by the env value, saw {bounds}"
        )


class TestKillHelpers:
    """The tree-kill decision logic, exercised with mocked process objects.

    Real Windows process killing cannot run on Linux, so what is under test is
    the decision: which platform branch is taken, whether the whole group (and
    not merely the direct child) is signalled, and whether SIGTERM escalates to
    SIGKILL.
    """

    def _fast_grace(self, monkeypatch, engine):
        """Shrink the post-SIGTERM patience so the escalation is instant.

        The helper reads ``_KILL_GRACE_SECONDS`` at call time for its second
        wait; the first one arrives as the ``grace`` argument, which the tests
        pass explicitly.
        """
        monkeypatch.setattr(engine, "_KILL_GRACE_SECONDS", 0.01)

    def _record_group_signals(self, monkeypatch, engine, pgid=4321, getpgid=None):
        calls = []
        monkeypatch.setattr(
            engine.os, "getpgid", getpgid or (lambda pid: pgid)
        )
        monkeypatch.setattr(
            engine.os, "killpg", lambda group, sig: calls.append((group, sig))
        )
        return calls

    def test_the_posix_helper_signals_the_whole_group_then_escalates(
        self, monkeypatch
    ):
        """SIGTERM to the pgid, then SIGKILL to the same pgid."""
        engine = render_engine_module()
        self._fast_grace(monkeypatch, engine)
        calls = self._record_group_signals(monkeypatch, engine)
        # exit_after=1: the child ignores the graceful signal, as chrome does.
        proc = _FakeProc(returncode=None, exit_after=1)

        asyncio.run(engine._kill_posix_process_tree(proc, grace=0.01))

        assert calls == [
            (4321, signal.SIGTERM),
            (4321, signal.SIGKILL),
        ], "both signals must reach the process group"
        assert proc.signals == [], (
            "signalling only the direct child is the orphaning bug itself"
        )

    def test_a_child_that_exits_politely_is_not_killed(self, monkeypatch):
        engine = render_engine_module()
        self._fast_grace(monkeypatch, engine)
        calls = self._record_group_signals(monkeypatch, engine)
        proc = _FakeProc(returncode=None, exit_after=0)

        asyncio.run(engine._kill_posix_process_tree(proc, grace=0.5))

        assert calls == [(4321, signal.SIGTERM)], (
            "a render that honoured SIGTERM must not also be SIGKILLed"
        )

    def test_an_unreadable_group_falls_back_to_the_direct_child(self, monkeypatch):
        """A pid with no group left still has to get the signal."""
        engine = render_engine_module()
        self._fast_grace(monkeypatch, engine)

        def no_such_group(pid):
            raise ProcessLookupError(pid)

        self._record_group_signals(monkeypatch, engine, getpgid=no_such_group)
        proc = _FakeProc(returncode=None, exit_after=0)

        asyncio.run(engine._kill_posix_process_tree(proc, grace=0.5))

        assert proc.signals == [signal.SIGTERM]
        assert not proc.killed

    def test_a_sigkill_that_never_lands_is_backstopped_synchronously(
        self, monkeypatch
    ):
        """A second cancellation mid-cleanup must still not leave chrome alive."""
        engine = render_engine_module()
        self._fast_grace(monkeypatch, engine)

        def no_such_group(pid):
            raise ProcessLookupError(pid)

        self._record_group_signals(monkeypatch, engine, getpgid=no_such_group)
        proc = _FakeProc(returncode=None, exit_after=5)  # never exits

        asyncio.run(engine._kill_process_tree(proc, grace=0.01))

        assert proc.killed, "the finally in _kill_process_tree must force the child down"

    def test_the_windows_helper_terminates_then_taskkills_the_whole_tree(
        self, monkeypatch
    ):
        """proc.kill() alone leaves chrome and ffmpeg running on Windows."""
        engine = render_engine_module()
        self._fast_grace(monkeypatch, engine)
        spawns = []

        async def fake_exec(*args, **kwargs):
            spawns.append((list(args), kwargs))
            return _FakeProc(returncode=0)

        monkeypatch.setattr(engine.asyncio, "create_subprocess_exec", fake_exec)
        proc = _FakeProc(returncode=None, exit_after=1)  # ignores terminate()

        asyncio.run(engine._kill_windows_process_tree(proc, grace=0.01))

        assert proc.terminated is True, "a graceful terminate always goes first"
        argv, _kwargs = spawns[0]
        assert argv == ["taskkill", "/PID", str(proc.pid), "/T", "/F"], (
            "/T walks the tree and /F is what chrome actually answers to"
        )

    def test_a_windows_child_that_terminates_needs_no_taskkill(self, monkeypatch):
        engine = render_engine_module()
        self._fast_grace(monkeypatch, engine)
        spawns = []

        async def fake_exec(*args, **kwargs):
            spawns.append((list(args), kwargs))
            return _FakeProc(returncode=0)

        monkeypatch.setattr(engine.asyncio, "create_subprocess_exec", fake_exec)
        proc = _FakeProc(returncode=None, exit_after=0)

        asyncio.run(engine._kill_windows_process_tree(proc, grace=0.5))

        assert proc.terminated is True
        assert spawns == [], "a polite exit must not reach the forcible path"

    def test_the_dispatch_follows_the_platform(self, monkeypatch):
        """Both branches stay reachable from a Linux run, via _is_windows."""
        engine = render_engine_module()
        chosen = []

        async def fake_posix(proc, **kwargs):
            chosen.append("posix")

        async def fake_windows(proc, **kwargs):
            chosen.append("windows")

        monkeypatch.setattr(engine, "_kill_posix_process_tree", fake_posix)
        monkeypatch.setattr(engine, "_kill_windows_process_tree", fake_windows)

        monkeypatch.setattr(engine, "_is_windows", lambda: True)
        asyncio.run(engine._kill_process_tree(_FakeProc(returncode=None)))
        monkeypatch.setattr(engine, "_is_windows", lambda: False)
        asyncio.run(engine._kill_process_tree(_FakeProc(returncode=None)))

        assert chosen == ["windows", "posix"]

    def test_a_process_that_already_exited_is_left_alone(self, monkeypatch):
        """Reaping twice would raise, and signalling a dead pid is noise."""
        engine = render_engine_module()
        calls = self._record_group_signals(monkeypatch, engine)

        asyncio.run(engine._kill_process_tree(_FakeProc(returncode=0)))

        assert calls == []

    def test_the_kill_never_raises_into_the_failure_path(self, monkeypatch):
        """A kill that blows up must not replace the real render failure."""
        engine = render_engine_module()
        self._fast_grace(monkeypatch, engine)

        def forbidden(pgid, sig):
            raise PermissionError("nope")

        self._record_group_signals(monkeypatch, engine)
        monkeypatch.setattr(engine.os, "killpg", forbidden)
        proc = _FakeProc(returncode=None, exit_after=1)

        asyncio.run(engine._kill_process_tree(proc, grace=0.01))


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="asserts on /proc state, which is Linux-only",
)
class TestWatchdogAgainstARealProcess:
    """One real subprocess group, so "no leak" is a measurement not a claim.

    The rest of this file fakes the process object. This launches a stand-in
    render - no npx, no HyperFrames, no minutes of waiting - that forks a
    grandchild the way chrome-headless-shell forks from npx, then hangs. After
    the watchdog fires, neither may still be running.
    """

    def _stand_in_render(self, tmp_path):
        """An executable that records its own pid plus a hung grandchild's."""
        script = tmp_path / "fake_render.py"
        # A shebang pointing at the running interpreter keeps this portable
        # across whatever python the suite was launched with.
        script.write_text(
            f"#!{sys.executable}\n"
            "import os, sys, time\n"
            "marker = os.environ['FAKE_RENDER_MARKER']\n"
            "grandchild = os.fork()\n"
            "if grandchild == 0:\n"
            "    time.sleep(600)\n"
            "    os._exit(0)\n"
            "with open(marker, 'w') as handle:\n"
            "    handle.write('%d %d' % (os.getpid(), grandchild))\n"
            "time.sleep(600)\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
        return script

    @staticmethod
    def _still_running(pid):
        """True only for a live, non-zombie process."""
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        try:
            state = pathlib.Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1][0]
        except OSError:
            return False
        return state != "Z"

    def test_a_hung_render_and_its_grandchild_are_both_gone(self, monkeypatch, tmp_path):
        engine = render_engine_module()
        monkeypatch.setenv("DARK_STUDIO_RENDER_TIMEOUT", "0.5")
        marker = tmp_path / "pids.txt"
        monkeypatch.setenv("FAKE_RENDER_MARKER", str(marker))
        monkeypatch.setattr(engine, "HYPERFRAMES_CLI", str(self._stand_in_render(tmp_path)))
        output = tmp_path / "proj.mp4"

        async def main():
            with pytest.raises(RuntimeError, match="tempo limite"):
                await engine.render_with_hyperframes(
                    "proj", "<html></html>", output, tmp_path, 1080, 1920
                )

        started = time.time()
        asyncio.run(main())
        assert time.time() - started < 30, "the watchdog must fire without waiting it out"

        assert marker.exists(), "the stand-in render never started"
        parent, grandchild = (int(value) for value in marker.read_text().split())
        assert not self._still_running(parent), "npx is still running after the timeout"
        assert not self._still_running(grandchild), (
            "chrome-headless-shell survived: only the direct child was killed"
        )
        assert not output.exists()


class _FakeProc:
    """A stand-in for :class:`asyncio.subprocess.Process`.

    ``hang=True`` makes ``communicate()`` block forever, which is what a render
    that outruns its budget looks like; it creates the output file first, because
    a real renderer does. ``exit_after`` counts how many ``wait()`` calls the
    process survives: 0 exits at once, 1 ignores the graceful signal (as chrome
    does), 5 never exits within a test's patience.
    """

    def __init__(self, stdout=b"", stderr=b"", returncode=0, hang=False, exit_after=0):
        self.pid = 4321
        self.stdout_bytes = stdout
        self.stderr_bytes = stderr
        self.returncode = returncode
        self.hang = hang
        self._waits_left = exit_after
        self.signals = []
        self.terminated = False
        self.killed = False
        self.writes_output = None

    async def communicate(self):
        if self.writes_output is not None:
            pathlib.Path(self.writes_output).write_bytes(b"partial-mp4")
        if self.hang:
            await asyncio.Event().wait()
        return self.stdout_bytes, self.stderr_bytes

    async def wait(self):
        if self._waits_left > 0:
            self._waits_left -= 1
            await asyncio.sleep(3600)  # never returns inside a test
        if self.returncode is None:
            self.returncode = -9
        return self.returncode

    def send_signal(self, sig):
        self.signals.append(sig)

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True
        if self.returncode is None:
            self.returncode = -9


