"""Tests for the shorts pipeline.

The module shipped with no caller and could not run: it called the async renderer
without awaiting it, its audio lookup excluded every audio extension, and its
karaoke script referenced an undefined JS variable. These tests pin the fixed
behaviour, including the cases that previously crashed or lied.
"""
import asyncio
import inspect
import json
import re

import pytest

from backend.services import shorts_pipeline as sp
from backend.services.render_engine import generate_composition_html


def _segments(rows):
    return [
        {"index": i + 1, "start": start, "end": end, "text": text}
        for i, (start, end, text) in enumerate(rows)
    ]


SAMPLE = _segments([
    (0.0, 6.0, "Voce trabalha cerca de duas mil horas por ano para accumulates."),
    (6.0, 12.0, "A explicacao superficial diz que o dinheiro sempre foi inventado assim."),
    (12.0, 20.0, "Nas proximas semanas quase ninguem continua a trocar apenas por uma nota."),
])


class TestAudioDiscovery:
    def test_finds_mp3(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sp, "UPLOAD_DIR", tmp_path)
        (tmp_path / "proj.mp3").write_bytes(b"\x00")
        found = sp.find_project_audio("proj")
        assert found is not None and found.suffix == ".mp3"

    def test_finds_wav(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sp, "UPLOAD_DIR", tmp_path)
        (tmp_path / "proj.wav").write_bytes(b"\x00")
        assert sp.find_project_audio("proj") is not None

    def test_ignores_non_audio_files(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sp, "UPLOAD_DIR", tmp_path)
        (tmp_path / "proj.srt").write_text("x", encoding="utf-8")
        (tmp_path / "proj.json").write_text("{}", encoding="utf-8")
        (tmp_path / "proj.mp4").write_bytes(b"\x00")
        assert sp.find_project_audio("proj") is None, (
            "only audio extensions may be picked up as narration"
        )

    def test_returns_none_when_absent(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sp, "UPLOAD_DIR", tmp_path)
        assert sp.find_project_audio("nada") is None


class TestHighlightDetection:
    def test_falls_back_when_no_segments(self):
        assert sp.detect_highlights([], "qualquer coisa") == []

    def test_fallback_returns_usable_spans(self):
        highlights = sp._fallback_highlight_detection(SAMPLE)
        assert highlights
        for item in highlights:
            assert item["end"] > item["start"]
            assert item["text"]
            assert 0.0 <= item["importance"] <= 1.0

    def test_fallback_respects_short_form_limits(self):
        long_segment = _segments([(0.0, 600.0, "uma frase muito longa sobre o tema do video." * 3)])
        highlights = sp._fallback_highlight_detection(long_segment)
        for item in highlights:
            duration = item["end"] - item["start"]
            assert duration <= sp.MAX_HIGHLIGHT_SECONDS + 0.01

    def test_fallback_is_capped(self):
        many = _segments([(i * 5.0, i * 5.0 + 4.0, f"frase numero {i} com conteudo suficiente.") for i in range(30)])
        assert len(sp._fallback_highlight_detection(many)) <= sp.MAX_HIGHLIGHTS

    def test_llm_output_is_normalised(self, monkeypatch):
        import backend.services.pipeline as pipeline

        monkeypatch.setattr(pipeline, "call_free_model", lambda prompt, **kwargs: json.dumps([
            {"start_time": 0.0, "end_time": 6.0, "text": "primeiro", "importance": 0.9},
            {"start_time": 6.0, "text": "segundo", "importance": 5.0},
            {"start_time": 12.0, "end_time": 3.0, "text": "invertido"},
            {"not_text": 1},
        ]))
        highlights = sp.detect_highlights(SAMPLE, "transcricao")
        texts = [item["text"] for item in highlights]
        assert "primeiro" in texts and "segundo" in texts
        # end <= start must be repaired, not emitted as a zero/negative span.
        for item in highlights:
            assert item["end"] > item["start"]
        assert all(item["importance"] <= 1.0 for item in highlights)

    def test_code_fenced_json_is_accepted(self, monkeypatch):
        import backend.services.pipeline as pipeline

        payload = '```json\n[{"start_time": 0.0, "end_time": 5.0, "text": "ok"}]\n```'
        monkeypatch.setattr(pipeline, "call_free_model", lambda prompt, **kwargs: payload)
        highlights = sp.detect_highlights(SAMPLE, "transcricao")
        assert [item["text"] for item in highlights] == ["ok"]

    def test_highlights_stay_inside_their_source_segment(self, monkeypatch):
        import backend.services.pipeline as pipeline

        monkeypatch.setattr(pipeline, "call_free_model", lambda prompt, **kwargs: json.dumps([
            {"start_time": 5.5, "end_time": 11.5, "text": "dentro do primeiro"},
            {"start_time": 999.0, "end_time": 1000.0, "text": "fora do video"},
        ]))
        highlights = sp.detect_highlights(SAMPLE, "transcricao")
        assert len(highlights) == 1, "a highlight outside every segment must be dropped"

        bounds = [(s["start"], s["end"]) for s in SAMPLE]
        for highlight in highlights:
            assert any(
                low <= highlight["start"] and highlight["end"] <= high
                for low, high in bounds
            ), f"{highlight} escaped its source segment {bounds}"

    def test_non_list_model_output_falls_back(self, monkeypatch):
        import backend.services.pipeline as pipeline

        monkeypatch.setattr(pipeline, "call_free_model", lambda prompt, **kwargs: '{"angle": "nao e um array"}')
        assert sp.detect_highlights(SAMPLE, "transcricao")

    def test_provider_failure_falls_back(self, monkeypatch):
        import backend.services.pipeline as pipeline

        def boom(prompt, **kwargs):
            raise RuntimeError("OPENROUTER_API_KEY não configurada.")

        monkeypatch.setattr(pipeline, "call_free_model", boom)
        assert sp.detect_highlights(SAMPLE, "transcricao")


class TestWordTimings:
    def test_words_are_ordered_and_non_overlapping(self):
        words = sp.extract_words_with_timestamps(SAMPLE)
        assert len(words) > 20
        for previous, current in zip(words, words[1:]):
            assert current["start"] >= previous["end"] - 1e-6

    def test_word_spans_tile_the_segment(self):
        words = sp.extract_words_with_timestamps([SAMPLE[0]])
        assert words[0]["start"] == pytest.approx(SAMPLE[0]["start"])
        assert words[-1]["end"] == pytest.approx(SAMPLE[0]["end"])

    def test_every_word_has_positive_duration(self):
        for word in sp.extract_words_with_timestamps(SAMPLE):
            assert word["end"] > word["start"]

    def test_empty_segments_produce_no_words(self):
        assert sp.extract_words_with_timestamps([{"index": 1, "start": 0, "end": 1, "text": ""}]) == []

    def test_zero_length_segment_is_repaired(self):
        words = sp.extract_words_with_timestamps([{"index": 1, "start": 5.0, "end": 5.0, "text": "ola mundo"}])
        assert words and all(word["end"] > word["start"] for word in words)

    def test_words_for_range_selects_by_midpoint(self):
        words = sp.extract_words_with_timestamps(SAMPLE)
        selected = sp.words_for_range(words, 6.0, 12.0)
        assert selected
        assert all(6.0 <= (w["start"] + w["end"]) / 2 < 12.0 for w in selected)


class TestStoryboard:
    def test_rebase_runs_scenes_back_to_back(self):
        highlights = sp._fallback_highlight_detection(SAMPLE)
        words = sp.extract_words_with_timestamps(SAMPLE)
        scenes = sp.build_highlight_storyboard(highlights, words, [], rebase=True)
        assert scenes
        assert scenes[0]["start"] == 0.0
        for previous, current in zip(scenes, scenes[1:]):
            assert current["start"] == pytest.approx(previous["end"])

    def test_rebase_removes_the_gaps_between_highlights(self):
        highlights = [
            {"start": 0.0, "end": 4.0, "text": "a", "importance": 0.5},
            {"start": 30.0, "end": 34.0, "text": "b", "importance": 0.5},
        ]
        scenes = sp.build_highlight_storyboard(highlights, [], [], rebase=True)
        assert scenes[-1]["end"] == pytest.approx(8.0), "a cut must not inherit the source gap"

    def test_rebase_shifts_word_timings_too(self):
        highlights = [{"start": 20.0, "end": 26.0, "text": "destacado", "importance": 0.9}]
        words = sp.extract_words_with_timestamps([{"index": 1, "start": 20.0, "end": 26.0, "text": "destacado"}])
        scenes = sp.build_highlight_storyboard(highlights, words, [], rebase=True)
        scene = scenes[0]
        assert scene["start"] == 0.0
        for word in scene["words"]:
            assert scene["start"] <= word["start"] and word["end"] <= scene["end"]

    def test_source_timestamps_are_preserved_for_audit(self):
        highlights = [{"start": 20.0, "end": 26.0, "text": "x", "importance": 0.9}]
        scene = sp.build_highlight_storyboard(highlights, [], [], rebase=True)[0]
        assert scene["source_start"] == 20.0 and scene["source_end"] == 26.0

    def test_without_rebase_timings_match_the_source(self):
        highlights = [{"start": 5.0, "end": 9.0, "text": "x", "importance": 0.5}]
        scene = sp.build_highlight_storyboard(highlights, [], [], rebase=False)[0]
        assert scene["start"] == 5.0 and scene["end"] == 9.0

    def test_karaoke_is_used_only_when_words_exist(self):
        words = sp.extract_words_with_timestamps(SAMPLE)
        with_words = sp.build_highlight_storyboard(
            [{"start": 0.0, "end": 6.0, "text": "a", "importance": 0.5}], words, []
        )[0]
        assert with_words["caption_style"] == "karaoke"
        without = sp.build_highlight_storyboard(
            [{"start": 0.0, "end": 6.0, "text": "a", "importance": 0.5}], [], []
        )[0]
        assert without["caption_style"] == "bottom"

    def test_media_is_attached_when_available(self):
        media = [{"url": "https://example.test/a.jpg"}, {"url": "https://example.test/b.jpg"}]
        highlights = [
            {"start": 0.0, "end": 4.0, "text": "a", "importance": 0.5},
            {"start": 4.0, "end": 8.0, "text": "b", "importance": 0.5},
        ]
        scenes = sp.build_highlight_storyboard(highlights, [], media)
        assert scenes[0]["media_url"] == "https://example.test/a.jpg"
        assert scenes[1]["media_url"] == "https://example.test/b.jpg"

    def test_no_media_means_empty_url_not_a_fake_one(self):
        scene = sp.build_highlight_storyboard(
            [{"start": 0.0, "end": 4.0, "text": "a", "importance": 0.5}], [], []
        )[0]
        assert scene["media_url"] == ""


class TestKaraokeHtml:
    def test_no_undefined_js_references(self):
        highlights = sp._fallback_highlight_detection(SAMPLE)
        html = sp.create_karaoke_html(highlights)
        assert "undefined" not in html, "the karaoke script referenced an undefined variable"
        assert "${" not in html, "unrendered JS template literal in the output"

    def test_timeline_is_paused_and_registered(self):
        html = sp.create_karaoke_html(sp._fallback_highlight_detection(SAMPLE))
        assert "gsap.timeline({ paused: true })" in html
        assert 'window.__timelines["main"] = tl' in html

    def test_no_infinite_repeat(self):
        html = sp.create_karaoke_html(sp._fallback_highlight_detection(SAMPLE))
        assert "repeat: -1" not in html

    def test_empty_highlights_still_produce_a_document(self):
        html = sp.create_karaoke_html([])
        assert "<html" in html and "data-composition-id" in html

    def test_caption_text_is_escaped(self):
        highlights = [{"start": 0.0, "end": 4.0, "text": '<img src=x onerror="alert(1)">', "importance": 0.5}]
        html = sp.create_karaoke_html(highlights)
        assert "<img src=x" not in html, "a caption must not be able to inject markup"
        assert "&lt;img" in html


class TestGenerateShortsContract:
    def test_generate_shorts_is_a_coroutine(self):
        """It must be awaited; the old sync version silently produced a coroutine."""
        assert inspect.iscoroutinefunction(sp.generate_shorts)

    def test_renderer_is_awaited_in_the_source(self):
        source = inspect.getsource(sp.generate_shorts)
        assert "await render_video_hyperframes" in source

    def test_missing_srt_reports_an_error_code(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sp, "UPLOAD_DIR", tmp_path)
        result = asyncio.run(sp.generate_shorts("fantasma"))
        assert result["status"] == "error"
        assert result["error_code"] == "SHORTS_NO_SRT"

    def test_empty_srt_reports_an_error_code(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sp, "UPLOAD_DIR", tmp_path)
        (tmp_path / "vazio.srt").write_text("", encoding="utf-8")
        result = asyncio.run(sp.generate_shorts("vazio"))
        assert result["error_code"] == "SHORTS_NO_SEGMENTS"

    def test_render_uses_a_separate_output_stem(self):
        source = inspect.getsource(sp.generate_shorts)
        assert 'output_stem=f"{project_name}_shorts"' in source, (
            "a shorts render must not overwrite the project's main video"
        )

    def test_cut_renders_without_the_source_narration(self):
        source = inspect.getsource(sp.generate_shorts)
        # Passing the full narration would play audio that does not match the cut.
        assert "scenes,\n        None," in source or "scenes, None," in source


class TestShortsRenderAsksForDeliberateSilence:
    """The shorts cut passes no track and asks for none.

    generate_shorts names no music_track and never calls music.pick_track (there
    is no music reference anywhere in shorts_pipeline.py), so the empty
    music_track it hands the renderer means silence by design - the cut is
    captions with no bed. This pins the music_auto value it sends, because a wrong
    True would have the render blame the library for a cut that was always going
    to be silent.
    """

    def _srt(self, tmp_path):
        text = "\n\n".join(
            f"{item['index']}\n"
            f"00:00:{int(item['start']):02d},000 --> 00:00:{int(item['end']):02d},000\n"
            f"{item['text']}"
            for item in SAMPLE
        )
        (tmp_path / "corte.srt").write_text(text, encoding="utf-8")
        return tmp_path / "corte.srt"

    def test_the_render_is_asked_for_deliberate_silence(self, tmp_path, monkeypatch):
        from backend.services import pipeline as pipeline_module
        from backend.services import render_engine as engine_module

        self._srt(tmp_path)
        monkeypatch.setattr(sp, "UPLOAD_DIR", tmp_path)
        monkeypatch.setattr(sp, "OUTPUT_DIR", tmp_path)
        monkeypatch.setattr(pipeline_module, "search_media_for_keywords", lambda kw: [])
        monkeypatch.setattr(
            sp, "detect_highlights",
            lambda segments, transcript="", niche="general": [
                {"start": 0.0, "end": 6.0, "text": SAMPLE[0]["text"], "importance": 0.9},
            ],
        )

        seen = []

        async def fake_render(*args, **kwargs):
            seen.append(kwargs)
            return {"status": "rendered", "output_path": str(tmp_path / "corte_shorts.mp4")}

        monkeypatch.setattr(engine_module, "render_video_hyperframes", fake_render)

        summary = asyncio.run(sp.generate_shorts("corte"))

        assert summary["status"] == "rendered"
        assert len(seen) == 1, "the cut must still render exactly once"
        assert "music_track" not in seen[0], (
            "this pipeline selects no track, so the renderer must not be told one"
        )
        assert seen[0]["music_auto"] is False, (
            "sending True here would report a library failure for silence by design"
        )

    def test_the_pipeline_selects_no_music_of_its_own(self):
        """The verdict above rests on this: no pick_track, no music_track.

        Read off the parsed module rather than the raw text, so the comments that
        explain the verdict do not count as uses of it.
        """
        import ast

        from pathlib import Path

        tree = ast.parse(Path(sp.__file__).read_text(encoding="utf-8"))
        referenced = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                referenced.add(node.attr)
            elif isinstance(node, ast.Name):
                referenced.add(node.id)
            elif isinstance(node, ast.keyword) and node.arg:
                referenced.add(node.arg)

        assert "pick_track" not in referenced, (
            "auto-BGM has been wired into this pipeline: the music_auto verdict "
            "has to be revisited with it"
        )
        assert "music_track" not in referenced, (
            "this pipeline now names a track, so it is no longer a caller that "
            "asks for deliberate silence"
        )


class TestListHighlights:
    def test_missing_srt(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sp, "UPLOAD_DIR", tmp_path)
        assert sp.list_highlights("nada")["status"] == "error"

    def test_reports_count_and_total(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sp, "UPLOAD_DIR", tmp_path)
        srt = "\n\n".join(
            f"{i + 1}\n00:00:{s:02d},000 --> 00:00:{e:02d},000\n{text}"
            for i, (s, e, text) in enumerate([
                (0, 6, "primeira frase com conteudo suficiente para um destaque"),
                (6, 12, "segunda frase tambem com conteudo suficiente"),
            ])
        )
        (tmp_path / "proj.srt").write_text(srt, encoding="utf-8")
        info = sp.list_highlights("proj")
        assert info["status"] == "ok"
        assert info["highlights_count"] >= 1
        assert info["total_seconds"] > 0


class TestKaraokeCompositionIntegration:
    def test_composition_renders_per_word_spans_with_spaces(self):
        words = sp.extract_words_with_timestamps(SAMPLE[:1])
        scene = {
            "index": 1, "start": 0.0, "end": 6.0, "duration": 6.0,
            "caption": " ".join(word["word"] for word in words),
            "caption_style": "karaoke", "words": words,
            "background": "#0f172a", "media_url": "",
        }
        html = generate_composition_html("t", [scene], {}, None, "vertical", 6.0)
        spans = re.findall(r'<span class="karaoke-word">([^<]*)</span>', html)
        assert len(spans) == len(words)
        # Whitespace between inline-block spans, or the words run together on screen.
        assert "</span> <span class=\"karaoke-word\"" in html

    def test_each_word_gets_its_own_tween(self):
        words = sp.extract_words_with_timestamps(SAMPLE[:1])
        scene = {
            "index": 1, "start": 0.0, "end": 6.0, "duration": 6.0,
            "caption": "palavras", "caption_style": "karaoke", "words": words,
            "background": "#0f172a", "media_url": "",
        }
        html = generate_composition_html("t", [scene], {}, None, "vertical", 6.0)
        for index in range(len(words)):
            assert f"nth-of-type({index + 1})" in html

    def test_word_spans_are_escaped(self):
        scene = {
            "index": 1, "start": 0.0, "end": 4.0, "duration": 4.0,
            "caption": "x", "caption_style": "karaoke",
            "words": [{"word": "<b>malicioso</b>", "start": 0.0, "end": 1.0}],
            "background": "#0f172a", "media_url": "",
        }
        html = generate_composition_html("t", [scene], {}, None, "vertical", 4.0)
        assert "<b>malicioso</b>" not in html
        assert "&lt;b&gt;malicioso&lt;/b&gt;" in html