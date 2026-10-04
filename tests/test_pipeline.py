"""Tests for backend/services/pipeline.py.

Two things are pinned here:

1. A transcript that is not real must be impossible to mistake for a real one.
   The bug this guards against was `except Exception: pass` followed by five
   hardcoded Portuguese sentences about routine and exercise, each 3 s long,
   returned as if whisper had produced them, with `degraded: false` on the way
   out. faster-whisper is an optional dependency, so that path was the normal
   path.
2. build_srt_from_text never invents, never over-claims the audio duration, and
   returns exactly the shape transcribe_audio_file returns.
"""
from __future__ import annotations

import inspect
import sys
import types

import pytest

from backend.services import pipeline


REAL_DURATION = 12.384  # the duration of the 27 MB production render


# --------------------------------------------------------------------- fakes
class _FakeSegment:
    def __init__(self, start, end, text):
        self.start = start
        self.end = end
        self.text = text


def _real_segments():
    return [
        _FakeSegment(0.0, 4.0, " As pessoas passam o dia a correr. "),
        _FakeSegment(4.0, 8.5, ""),
        _FakeSegment(8.5, REAL_DURATION, "O corpo humano foi feito para se mover."),
    ]


def _install_fake_whisper(monkeypatch, segments=None, error=None):
    """Register a stand-in faster_whisper module.

    `error` makes WhisperModel() itself fail (a missing onnxruntime, a bad model
    download); `segments` makes it return a lazy generator that fails, or yields
    nothing, while iterating - the way the real package behaves.
    """
    module = types.ModuleType("faster_whisper")

    def _model_factory(_name, device=None, compute_type=None):
        if error is not None:
            raise error

        def _transcribe(*_args, **_kwargs):
            def _generate():
                for seg in segments or []:
                    yield seg

            if isinstance(error, BaseException):
                raise error
            return _generate(), object()

        return types.SimpleNamespace(transcribe=_transcribe)

    module.WhisperModel = _model_factory
    monkeypatch.setitem(sys.modules, "faster_whisper", module)
    return module


def _hide_whisper(monkeypatch):
    """Make `from faster_whisper import WhisperModel` raise ImportError."""
    monkeypatch.setitem(sys.modules, "faster_whisper", None)


@pytest.fixture
def audio(tmp_path):
    path = tmp_path / "narracao.mp3"
    path.write_bytes(b"ID3-not-a-real-mp3")
    return path


@pytest.fixture
def known_duration(monkeypatch):
    monkeypatch.setattr(pipeline, "get_media_duration", lambda _path: REAL_DURATION)


# ------------------------------------------------- no fabricated transcription
class TestTranscriptionIsNeverFabricated:
    def test_missing_whisper_is_visible_to_the_caller(self, monkeypatch, audio, known_duration):
        _hide_whisper(monkeypatch)

        segments = pipeline.transcribe_audio_file(audio)

        assert segments, "a placeholder still has to be usable downstream"
        assert pipeline.is_placeholder_transcript(segments) is True
        assert pipeline.transcript_issue(segments)["reason"] == pipeline.REASON_WHISPER_MISSING

    def test_placeholder_text_is_not_plausible_narration(self, monkeypatch, audio, known_duration):
        _hide_whisper(monkeypatch)

        segments = pipeline.transcribe_audio_file(audio)
        text = " ".join(seg["text"] for seg in segments)

        # Marked as a placeholder in the data itself, not just in a flag.
        assert text == pipeline.PLACEHOLDER_TEXT
        assert "SEM TRANSCRIÇÃO" in text
        assert pipeline.INSTALL_HINT in text
        # Survives the 80-character caption truncation applied downstream, so it
        # is still visible in the rendered video.
        assert len(text) <= 80

    def test_the_hardcoded_filler_is_gone_from_the_module(self):
        source = inspect.getsource(pipeline)
        for phrase in (
            "A rotina moderna nos consome em excesso.",
            "passam o dia correndo contra o tempo",
            "O corpo humano foi feito para se mover.",
            "A atividade física melhora a saúde mental.",
            "A consistência é mais importante do que intensidade.",
        ):
            assert phrase not in source, f"fabricated filler still present: {phrase!r}"

    def test_storyboard_from_a_placeholder_says_so(self, monkeypatch, audio, known_duration):
        _hide_whisper(monkeypatch)
        segments = pipeline.transcribe_audio_file(audio)

        storyboard = pipeline.build_storyboard_from_segments(segments)

        assert len(storyboard) == 1
        assert "SEM TRANSCRIÇÃO" in storyboard[0]["caption"]
        # The AI term extractor is fed the caption, so it must never be handed
        # plausible-looking filler about a topic nobody narrated.
        assert "correndo" not in storyboard[0]["caption"]

    def test_real_transcript_is_not_marked(self, monkeypatch, audio, known_duration):
        _install_fake_whisper(monkeypatch, segments=_real_segments())

        segments = pipeline.transcribe_audio_file(audio)

        assert pipeline.is_placeholder_transcript(segments) is False
        assert pipeline.transcript_issue(segments) is None
        assert [seg["text"] for seg in segments] == [
            "As pessoas passam o dia a correr.",
            "O corpo humano foi feito para se mover.",
        ]
        # Shape is exactly what build_srt_from_segments and the storyboard read.
        for seg in segments:
            assert set(seg) == {"index", "start", "end", "text"}


# ------------------------------------------------------------ honest degradation
class TestDegradationIsHonest:
    def test_placeholder_report_is_degraded_and_says_how_to_fix_it(
        self, monkeypatch, audio, known_duration
    ):
        _hide_whisper(monkeypatch)
        segments = pipeline.transcribe_audio_file(audio)

        status = pipeline.transcription_status(segments)

        assert status["degraded"] is True
        assert status["transcript_source"] == "placeholder"
        for field in ("warning", "fallback_error"):
            message = status[field]
            # Names the real cause...
            assert "faster-whisper" in message
            # ...and how to fix it.
            assert pipeline.INSTALL_HINT in message
            # A user has to be able to read it.
            assert any(ord(char) > 127 for char in message), message

    def test_real_transcript_is_not_degraded(self, monkeypatch, audio, known_duration):
        _install_fake_whisper(monkeypatch, segments=_real_segments())
        segments = pipeline.transcribe_audio_file(audio)

        status = pipeline.transcription_status(segments)

        assert status["degraded"] is False
        assert status["warning"] is None
        assert status["fallback_error"] is None
        assert status["transcript_source"] == "faster-whisper"

    def test_empty_transcript_is_degraded_not_a_success(self):
        status = pipeline.transcription_status([])

        assert status["degraded"] is True
        assert status["transcript_source"] == "none"
        assert pipeline.INSTALL_HINT in status["warning"]

    def test_degraded_false_can_never_accompany_a_placeholder(self, monkeypatch, audio, known_duration):
        _hide_whisper(monkeypatch)
        segments = pipeline.transcribe_audio_file(audio)

        # The exact failure from production: a payload that says everything is
        # fine while the captions were invented.
        payload = {"degraded": False}
        payload.update(pipeline.transcription_status(segments))

        assert payload["degraded"] is True
        assert payload["warning"]

    def test_opt_in_hard_failure_for_callers_that_refuse_a_placeholder(
        self, monkeypatch, audio
    ):
        _hide_whisper(monkeypatch)

        with pytest.raises(pipeline.TranscriptionDependencyMissing) as excinfo:
            pipeline.transcribe_audio_file(audio, allow_placeholder=False)

        assert pipeline.INSTALL_HINT in str(excinfo.value)


# ------------------------------------------------------------- real failures surface
class TestGenuineFailuresAreNotSwallowed:
    def test_model_construction_failure_surfaces_the_real_error(self, monkeypatch, audio):
        boom = RuntimeError("onnxruntime is not installed")
        _install_fake_whisper(monkeypatch, error=boom)

        with pytest.raises(pipeline.TranscriptionFailed) as excinfo:
            pipeline.transcribe_audio_file(audio)

        assert "onnxruntime is not installed" in str(excinfo.value)
        assert excinfo.value.__cause__ is boom

    def test_failure_while_decoding_surfaces_the_real_error(self, monkeypatch, audio):
        # faster-whisper decodes lazily, so a corrupt file raises during
        # iteration, not at model.transcribe(). Both must be caught and reported.
        _install_fake_whisper(monkeypatch, error=ValueError("invalid audio data"))

        with pytest.raises(pipeline.TranscriptionFailed) as excinfo:
            pipeline.transcribe_audio_file(audio)

        assert "invalid audio data" in str(excinfo.value)

    def test_a_3_byte_mp3_does_not_produce_captions(self, monkeypatch, audio):
        _install_fake_whisper(monkeypatch, segments=[])

        with pytest.raises(pipeline.TranscriptionFailed) as excinfo:
            pipeline.transcribe_audio_file(audio)

        assert pipeline.INSTALL_HINT in str(excinfo.value)

    def test_transcription_error_is_a_runtime_error_for_old_callers(self):
        # app.py wraps the call in `except Exception` and answers 422.
        assert issubclass(pipeline.TranscriptionFailed, RuntimeError)
        assert issubclass(pipeline.TranscriptionDependencyMissing, pipeline.TranscriptionError)


# ------------------------------------------------------------------- timings
class TestFallbackTimingsRespectTheAudio:
    def test_placeholder_never_claims_more_than_the_audio_has(
        self, monkeypatch, audio, known_duration
    ):
        _hide_whisper(monkeypatch)

        segments = pipeline.transcribe_audio_file(audio)

        assert segments[0]["start"] == 0.0
        assert segments[0]["end"] == pytest.approx(REAL_DURATION)
        for seg in segments:
            assert seg["end"] <= REAL_DURATION
            assert seg["end"] > seg["start"]
        assert segments[0]["duration_known"] is True

    def test_the_old_15_second_srt_can_no_longer_happen(self, monkeypatch, audio, known_duration, tmp_path):
        _hide_whisper(monkeypatch)
        segments = pipeline.transcribe_audio_file(audio)

        srt_path = tmp_path / "narracao.srt"
        srt_path.write_text(pipeline.build_srt_from_segments(segments), encoding="utf-8")
        reparsed = pipeline.parse_srt_to_segments(srt_path)

        assert reparsed, "the SRT has to stay usable"
        for seg in reparsed:
            assert seg["end"] <= REAL_DURATION

    def test_the_last_scene_is_not_truncated_to_a_sliver(self, monkeypatch, audio, known_duration):
        # The production symptom: a 15 s caption list over 12.384 s of audio left
        # the final scene with 0.384 s.
        _hide_whisper(monkeypatch)
        segments = pipeline.transcribe_audio_file(audio)

        storyboard = pipeline.build_storyboard_from_segments(segments)

        assert len(storyboard) == 1
        last = storyboard[-1]
        assert last["end"] == pytest.approx(REAL_DURATION)
        assert last["end"] - last["start"] >= REAL_DURATION - 0.001

    def test_unknown_duration_under_claims_instead_of_guessing(self, monkeypatch, audio):
        monkeypatch.setattr(pipeline, "get_media_duration", lambda _path: None)
        _hide_whisper(monkeypatch)

        segments = pipeline.transcribe_audio_file(audio)

        assert segments[0]["end"] == pipeline.PLACEHOLDER_FALLBACK_DURATION
        assert segments[0]["duration_known"] is False
        assert segments[0]["end"] <= pipeline.PLACEHOLDER_FALLBACK_DURATION

    def test_broken_ffprobe_does_not_break_the_placeholder(self, monkeypatch, audio):
        def _explode(_path):
            raise OSError("ffprobe missing")

        monkeypatch.setattr(pipeline, "get_media_duration", _explode)
        _hide_whisper(monkeypatch)

        segments = pipeline.transcribe_audio_file(audio)

        assert segments[0]["end"] == pipeline.PLACEHOLDER_FALLBACK_DURATION


# --------------------------------------------------------- build_srt_from_text
NARRATION = (
    "Dinheiro é a primeira coisa que muda quando perdemos o sono. "
    "Dormir mal custa dinheiro: menos foco, mais erros e decisões caras. "
    "A reserva de emergência é o seguro que ninguém vê e todos agradecem. "
    "Investir cedo, automate o dinheiro e revise os números todos os meses."
)


class TestBuildSrtFromText:
    def test_segment_shape_matches_transcribe_audio_file(self):
        segments = pipeline.build_srt_from_text(NARRATION, 30.0)

        assert segments
        for position, seg in enumerate(segments, start=1):
            assert set(seg) == {"index", "start", "end", "text"}
            assert seg["index"] == position
            assert isinstance(seg["start"], float)
            assert isinstance(seg["end"], float)

    def test_captions_are_literal_substrings_of_a_real_narration(self):
        segments = pipeline.build_srt_from_text(NARRATION, 30.0)

        assert len(segments) > 1, "a 4-sentence narration must not become one cue"
        for seg in segments:
            assert seg["text"] in NARRATION, f"not a substring: {seg['text']!r}"
            assert seg["text"].strip() == seg["text"]

    def test_captions_cover_every_word_in_order(self):
        # Stronger than "substring": nothing invented, nothing dropped, no reordering.
        segments = pipeline.build_srt_from_text(NARRATION, 30.0)

        rebuilt = " ".join(seg["text"] for seg in segments)

        assert rebuilt == NARRATION

    def test_captions_stay_substrings_with_paragraphs_and_line_breaks(self):
        multi_paragraph = NARRATION.replace(". ", ".\n\n", 1)

        segments = pipeline.build_srt_from_text(multi_paragraph, 30.0)

        assert " ".join(seg["text"] for seg in segments) == " ".join(multi_paragraph.split())
        for seg in segments:
            assert seg["text"] in " ".join(multi_paragraph.split())

    def test_timings_cover_the_whole_duration(self):
        segments = pipeline.build_srt_from_text(NARRATION, 30.0)

        assert segments[0]["start"] == 0.0
        assert segments[-1]["end"] == pytest.approx(30.0)
        # Contiguous: no gap, no overlap between consecutive captions.
        for previous, following in zip(segments, segments[1:]):
            assert following["start"] == pytest.approx(previous["end"])

    def test_timings_are_proportional_to_caption_length(self):
        # 300 s, so the 0.2 s per-caption floor (tested separately) is noise and
        # the distribution can be checked as strictly proportional.
        segments = pipeline.build_srt_from_text(NARRATION, 300.0)

        lengths = [len(seg["text"]) for seg in segments]
        spans = [seg["end"] - seg["start"] for seg in segments]
        for length, span in zip(lengths, spans):
            assert span == pytest.approx(length * sum(spans) / sum(lengths), rel=0.02)

    def test_every_caption_gets_at_least_the_floor_on_short_audio(self):
        # 30 s over 4 captions: the floor is 0.2 s each, so no caption may be
        # squeezed to nothing by the proportional split.
        segments = pipeline.build_srt_from_text(NARRATION, 30.0)

        for seg in segments:
            assert seg["end"] - seg["start"] >= pipeline.MIN_SRT_SEGMENT_SECONDS

    def test_no_zero_or_negative_length_ever(self):
        for duration in (30.0, 5.0, 0.5, 0.2, 0.0001, 1e-9):
            segments = pipeline.build_srt_from_text(NARRATION, duration)
            assert segments, f"no segments for duration={duration}"
            for seg in segments:
                assert seg["end"] > seg["start"], seg
                assert seg["end"] <= max(duration, pipeline.DEFAULT_SRT_DURATION)
            # Whatever happened, the narration was not silently dropped.
            assert " ".join(seg["text"] for seg in segments) == NARRATION

    def test_empty_text_returns_an_empty_list_instead_of_raising(self):
        for value in ("", "   ", "\n\n\t ", None):
            assert pipeline.build_srt_from_text(value, 30.0) == []

    def test_non_positive_duration_falls_back_to_a_small_positive_default(self):
        for duration in (0, -1, -0.5, None, "abc", float("nan"), float("inf")):
            segments = pipeline.build_srt_from_text(NARRATION, duration)
            assert segments, duration
            assert segments[-1]["end"] == pytest.approx(pipeline.DEFAULT_SRT_DURATION)
            for seg in segments:
                assert seg["end"] > seg["start"]

    def test_captions_split_at_roughly_max_chars(self):
        segments = pipeline.build_srt_from_text(NARRATION, 30.0, max_chars=40)

        assert len(segments) >= 4
        for seg in segments:
            # A single word longer than the limit is allowed to overflow; nothing
            # here is one word long.
            assert len(seg["text"]) <= 40, seg["text"]
        # A larger limit never produces more captions.
        assert len(pipeline.build_srt_from_text(NARRATION, 30.0, max_chars=200)) <= len(segments)

    def test_a_word_longer_than_max_chars_does_not_loop_forever(self):
        segments = pipeline.build_srt_from_text("a" * 300, 5.0, max_chars=10)

        assert [seg["text"] for seg in segments] == ["a" * 300]

    def test_an_over_long_sentence_is_word_packed(self):
        text = " ".join(["palavra"] * 40) + "."

        segments = pipeline.build_srt_from_text(text, 20.0, max_chars=30)

        assert len(segments) > 1
        assert " ".join(seg["text"] for seg in segments) == text
        for seg in segments:
            assert len(seg["text"]) <= 30

    def test_non_positive_max_chars_falls_back_to_the_default(self):
        segments = pipeline.build_srt_from_text(NARRATION, 30.0, max_chars=0)

        assert " ".join(seg["text"] for seg in segments) == NARRATION

    def test_result_is_writable_as_an_srt_and_parsed_back_identically(self, tmp_path):
        segments = pipeline.build_srt_from_text(NARRATION, 30.0)

        srt_path = tmp_path / "guiao.srt"
        srt_path.write_text(pipeline.build_srt_from_segments(segments), encoding="utf-8")
        reparsed = pipeline.parse_srt_to_segments(srt_path)

        assert [seg["text"] for seg in reparsed] == [seg["text"] for seg in segments]
        assert reparsed[-1]["end"] == pytest.approx(30.0, abs=0.001)

    def test_feeds_the_storyboard_without_fabricating(self):
        segments = pipeline.build_srt_from_text(NARRATION, 30.0)

        storyboard = pipeline.build_storyboard_from_segments(segments)

        assert len(storyboard) == len(segments)
        assert pipeline.is_placeholder_transcript(segments) is False
        for scene, seg in zip(storyboard, segments):
            assert seg["text"].startswith(scene["caption"][:20])
            assert scene["end"] <= 30.0
