"""Catalogue, offline library bootstrap and mixing contract for background music.

No network. Every test works inside tmp_path with music.MUSIC_DIR monkeypatched,
and the library is generated with 1-second stand-ins instead of the real 36-48s
tracks so the suite stays fast. A separate test asserts the real spec lengths
are the 30-90s the library contract requires, without rendering them.
"""
import dataclasses
import subprocess
import wave
from pathlib import Path

import pytest

from backend.services import music


SHORT_DURATION = 1.0


@pytest.fixture
def music_dir(tmp_path, monkeypatch):
    """An isolated MUSIC_DIR with the builtin library already bootstrapped."""
    target = tmp_path / "music"
    target.mkdir()
    monkeypatch.setattr(music, "MUSIC_DIR", target)
    monkeypatch.setattr(
        music,
        "BUILTIN_SPECS",
        tuple(dataclasses.replace(spec, duration=SHORT_DURATION) for spec in music.BUILTIN_SPECS),
    )
    monkeypatch.setattr(
        music, "_BUILTIN_BY_ID", {s.id: s for s in music.BUILTIN_SPECS}
    )
    music.ensure_builtin_library()
    return target


def _wav_bytes(path: Path) -> bytes:
    return Path(path).read_bytes()


def _is_riff_wave(blob: bytes) -> bool:
    return len(blob) >= 44 and blob[:4] == b"RIFF" and blob[8:12] == b"WAVE"


def _write_sample_wav(path: Path, seconds: float = 0.5) -> Path:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(44100)
        handle.writeframes(b"\x00\x00" * int(44100 * seconds) * 2)
    return path


# ------------------------------------------------------------------- catalogue


def test_moods_have_six_entries():
    assert len(music.MOODS) == 6
    assert set(music.MOODS) == {"ambient", "tension", "uplifting", "dark", "documentary", "lofi"}


def test_builtin_spec_durations_are_within_thirty_to_ninety_seconds():
    # The real (unshortened) library must ship usable track lengths.
    for spec in music.BUILTIN_SPECS:
        assert 30.0 <= spec.duration <= 90.0, spec.id
        assert spec.mood in music.MOODS
        assert spec.id.startswith(spec.mood)


def test_list_tracks_is_empty_for_a_fresh_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(music, "MUSIC_DIR", tmp_path / "missing-music")
    assert music.list_tracks() == []
    assert music.pick_track() is None


def test_ensure_builtin_library_covers_every_mood(music_dir):
    tracks = music.list_tracks()
    assert len(tracks) >= 6
    assert {track.mood for track in tracks} == set(music.MOODS)
    for track in tracks:
        assert track.builtin is True
        assert track.source == "builtin"
        assert track.title
        assert Path(track.path).parent == music_dir


def test_ensure_builtin_library_is_idempotent(music_dir):
    before = {p.name: (_wav_bytes(p), p.stat().st_mtime_ns) for p in music_dir.glob("*.wav")}
    assert len(before) >= 6

    tracks = music.ensure_builtin_library()

    assert len(tracks) == len(before)
    after = {p.name: (_wav_bytes(p), p.stat().st_mtime_ns) for p in music_dir.glob("*.wav")}
    assert set(after) == set(before)
    for name, (blob, mtime) in before.items():
        assert after[name] == (blob, mtime), f"{name} was rewritten"


def test_generated_files_are_riff_wave_and_not_tiny(music_dir):
    for path in music_dir.glob("*.wav"):
        blob = _wav_bytes(path)
        assert len(blob) > 1024, path.name
        assert _is_riff_wave(blob), path.name


def test_generation_is_deterministic(tmp_path):
    first = tmp_path / "first.wav"
    second = tmp_path / "second.wav"
    music.generate_builtin_track("lofi-nightfall", first, duration=SHORT_DURATION)
    music.generate_builtin_track("lofi-nightfall", second, duration=SHORT_DURATION)
    assert _is_riff_wave(_wav_bytes(first))
    assert _wav_bytes(first) == _wav_bytes(second)


def test_generate_builtin_track_rejects_unknown_id(tmp_path):
    with pytest.raises(ValueError):
        music.generate_builtin_track("no-such-track", tmp_path / "x.wav")


# ----------------------------------------------------------------- path safety


@pytest.mark.parametrize(
    "hostile",
    ["../secrets", "a/b", "..", "..\\..\\secrets", "/etc/passwd", "C:\\Windows\\win.ini",
     "sub/../../escape", "", "  ", "x\x00y", "a b", ".hidden", "C:evil"],
)
def test_get_track_rejects_unsafe_ids(hostile, music_dir):
    assert music.get_track(hostile) is None


def test_get_track_rejects_traversal_without_filesystem_access(music_dir, monkeypatch):
    def explode(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("get_track touched the filesystem for an unsafe id")

    monkeypatch.setattr(music, "list_tracks", explode)
    assert music.get_track("../secrets") is None


def test_get_track_returns_none_when_absent(music_dir):
    assert music.get_track("definitely-not-here") is None
    assert music.get_track(music.BUILTIN_SPECS[0].id).id == music.BUILTIN_SPECS[0].id


# --------------------------------------------------------------------- uploads


def test_register_upload_copies_the_file(music_dir, tmp_path):
    source = _write_sample_wav(tmp_path / "my_song.wav")
    track = music.register_upload(source, title="Minha Trilha", mood="lofi")

    assert track.builtin is False
    assert track.source == "upload"
    assert track.mood == "lofi"
    assert track.title == "Minha Trilha"
    assert Path(track.path).parent == music_dir
    assert Path(track.path).exists()
    assert Path(track.path).read_bytes() == source.read_bytes()
    assert music.get_track(track.id) is not None


def test_register_upload_defaults_to_ambient(tmp_path, monkeypatch):
    monkeypatch.setattr(music, "MUSIC_DIR", tmp_path / "m")
    track = music.register_upload(_write_sample_wav(tmp_path / "s.wav"))
    assert track.mood == "ambient"
    assert track.title == "s"


@pytest.mark.parametrize("name", ["payload.exe", "script.sh", "notes.txt", "clip"])
def test_register_upload_rejects_unsupported_extensions(name, tmp_path, monkeypatch):
    monkeypatch.setattr(music, "MUSIC_DIR", tmp_path / "m")
    bad = tmp_path / name
    bad.write_bytes(b"MZ" + b"\x00" * 64)
    with pytest.raises(ValueError):
        music.register_upload(bad)


def test_register_upload_rejects_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(music, "MUSIC_DIR", tmp_path / "m")
    with pytest.raises(ValueError):
        music.register_upload(tmp_path / "nope.wav")


def test_register_upload_title_cannot_escape_music_dir(music_dir, tmp_path):
    source = _write_sample_wav(tmp_path / "x.wav")
    track = music.register_upload(source, title="../../../etc/passwd")

    stored = Path(track.path).resolve()
    assert stored.parent == music_dir.resolve()
    assert "/" not in track.id and "\\" not in track.id
    assert ".." not in track.id
    assert not (music_dir.parent / "etc").exists()


def test_list_tracks_mixes_builtins_and_uploads(music_dir, tmp_path):
    upload = music.register_upload(_write_sample_wav(tmp_path / "u.mp3"), title="Extra", mood="tension")
    tracks = music.list_tracks()
    assert len(tracks) == len(music.BUILTIN_SPECS) + 1
    assert [t.id for t in tracks].index(upload.id) > 0  # builtins sort first

    tension = music.list_tracks("tension")
    assert [t.id for t in tension] == [t.id for t in tension if t.mood == "tension"]
    assert upload.id in {t.id for t in tension}
    assert all(t.mood == "tension" for t in tension)


def test_search_tracks_is_accent_and_case_insensitive(music_dir, tmp_path):
    upload = music.register_upload(_write_sample_wav(tmp_path / "s.wav"), title="Época Dourada", mood="ambient")
    by_title = {t.id for t in music.search_tracks("epoca")}
    assert upload.id in by_title
    assert upload.id in {t.id for t in music.search_tracks("ÉPOCA")}

    by_mood = {t.id for t in music.search_tracks("documentary")}
    assert by_mood == {s.id for s in music.BUILTIN_SPECS if s.mood == "documentary"}
    assert music.search_tracks("") == []
    assert music.search_tracks("nothing-matches-this") == []


def test_track_to_dict_shape(music_dir):
    payload = music.list_tracks()[0].to_dict()
    assert set(payload) == {"id", "title", "mood", "duration", "path", "builtin", "source"}
    assert isinstance(payload["duration"], float)


# ----------------------------------------------------------------- auto pick


def test_pick_track_returns_none_on_empty_library(tmp_path, monkeypatch):
    monkeypatch.setattr(music, "MUSIC_DIR", tmp_path / "empty")
    assert music.pick_track() is None
    assert music.pick_track("ambient") is None


def test_pick_track_respects_mood_and_exclusions(music_dir):
    chosen = music.pick_track("dark")
    assert chosen is not None and chosen.mood == "dark"

    all_ids = [t.id for t in music.list_tracks()]
    other = music.pick_track(exclude_ids=[all_ids[0]])
    assert other is None or other.id != all_ids[0]

    exhausted = music.pick_track(exclude_ids=all_ids)
    assert exhausted is None


# -------------------------------------------------------------------- mixing


def test_mix_parameters_defaults_and_keys():
    params = music.mix_parameters()
    assert set(params) == {
        "music_volume", "duck_voice", "voice_target_db", "music_target_db",
        "fade_in", "fade_out", "filter",
    }
    assert params["music_volume"] == pytest.approx(0.18)
    assert params["duck_voice"] is True
    assert params["voice_target_db"] == -6.0
    assert params["music_target_db"] == -18.0
    assert params["fade_in"] == 1.5
    assert params["fade_out"] == 2.5
    assert params["filter"] == "lowpass=f=8000"


@pytest.mark.parametrize("given,expected", [(-5.0, 0.0), (0.0, 0.0), (1.0, 1.0), (9.9, 1.0), (0.42, 0.42)])
def test_mix_parameters_clamps_volume(given, expected):
    assert music.mix_parameters(given)["music_volume"] == pytest.approx(expected)


def test_mix_parameters_survives_garbage_volume():
    assert music.mix_parameters("loud")["music_volume"] == pytest.approx(0.18)
    assert music.mix_parameters(None)["music_volume"] == pytest.approx(0.18)
    assert music.mix_parameters(float("nan"))["music_volume"] == pytest.approx(0.18)
    assert music.mix_parameters(0.3, duck_voice=False)["duck_voice"] is False


# ------------------------------------------------------------------- looping


def test_loop_to_duration_degrades_when_ffmpeg_is_missing(music_dir, tmp_path, monkeypatch):
    source = music.list_tracks()[0].path
    target = tmp_path / "looped.wav"

    def missing_binary(*args, **kwargs):
        raise FileNotFoundError("ffmpeg")

    monkeypatch.setattr(music.subprocess, "run", missing_binary)

    result = music.loop_to_duration(Path(source), 600.0, target)

    assert result == target
    assert target.exists()
    assert target.read_bytes() == Path(source).read_bytes()


def test_loop_to_duration_copies_when_source_is_long_enough(music_dir, tmp_path, monkeypatch):
    source = music.list_tracks()[0].path
    target = tmp_path / "copy.wav"
    monkeypatch.setattr(music, "_probe_duration", lambda path: 900.0)

    def must_not_run(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("ffmpeg was called for a track that already fits")

    monkeypatch.setattr(music.subprocess, "run", must_not_run)

    assert music.loop_to_duration(Path(source), 600.0, target) == target
    assert target.read_bytes() == Path(source).read_bytes()


def test_loop_to_duration_uses_one_ffmpeg_call_with_a_loop_and_fades(music_dir, tmp_path, monkeypatch):
    source = music.list_tracks()[0].path
    target = tmp_path / "looped.wav"
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[0] == "ffprobe":
            return subprocess.CompletedProcess(command, 0, stdout=str(SHORT_DURATION), stderr="")
        target.write_bytes(_wav_bytes(source))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(music.subprocess, "run", fake_run)

    assert music.loop_to_duration(Path(source), 300.0, target) == target
    ffmpeg_calls = [c for c in calls if c[0] == "ffmpeg"]
    assert len(ffmpeg_calls) == 1  # one ffprobe probe, exactly one ffmpeg render
    command = ffmpeg_calls[0]
    assert isinstance(command, list)
    assert "-stream_loop" in command and command[command.index("-stream_loop") + 1] == "-1"
    assert command[command.index("-t") + 1] == "300.000"
    filters = command[command.index("-af") + 1]
    assert "afade=t=in" in filters and "afade=t=out" in filters


def test_loop_to_duration_never_raises_on_missing_source(tmp_path, monkeypatch):
    def missing_binary(*args, **kwargs):
        raise FileNotFoundError("ffmpeg")

    monkeypatch.setattr(music.subprocess, "run", missing_binary)
    result = music.loop_to_duration(tmp_path / "gone.wav", 120.0, tmp_path / "out.wav")
    assert isinstance(result, Path)
