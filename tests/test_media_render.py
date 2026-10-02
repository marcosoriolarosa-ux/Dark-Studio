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