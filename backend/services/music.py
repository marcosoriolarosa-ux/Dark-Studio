"""Background music catalogue, procedural builtin library and mixing parameters.

The project ships no binary assets, so the "built-in library" is synthesised with
numpy at first start: royalty-free by construction, deterministic (fixed seed per
track) and generated in a couple of seconds. No network, no ffmpeg required.

Layout mirrors the rest of the service: storage/music/ holds both the generated
WAVs and uploads.json, the manifest describing user uploads. ffmpeg/ffprobe are
optional - every function degrades instead of raising.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import unicodedata
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from dotenv import load_dotenv

try:  # numpy is only needed to *generate* the builtin library.
    import numpy as np
except ImportError:  # pragma: no cover - only on a clone without numpy
    np = None  # type: ignore[assignment]

BASE_DIR = Path(__file__).resolve().parents[2]
# Same rationale as pipeline.py: the .env file is project-scoped config the
# settings UI writes to, and a stale ambient variable must not shadow it.
load_dotenv(BASE_DIR / ".env", override=True)
STORAGE_DIR = BASE_DIR / "storage"
MUSIC_DIR = STORAGE_DIR / "music"

MUSIC_DIR.mkdir(parents=True, exist_ok=True)

MANIFEST_NAME = "uploads.json"
SAMPLE_RATE = 44100
# ~ -3 dBFS. Every generated track is peak-normalised to this, so the mixer
# cannot clip even at music_volume 1.0.
TARGET_PEAK = 10 ** (-3.0 / 20.0)
FADE_IN = 1.5
FADE_OUT = 2.5

AUDIO_SUFFIXES = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac", ".opus"}

MOODS = ("ambient", "tension", "uplifting", "dark", "documentary", "lofi")

DEFAULT_MOOD = "ambient"
DEFAULT_MUSIC_VOLUME = 0.18

# Mixing contract handed to the render engine. Module constants so the mixer and
# mix_parameters() can never drift apart.
VOICE_TARGET_DB = -6.0
MUSIC_TARGET_DB = -18.0
MIX_FILTER = "lowpass=f=8000"


@dataclass
class Track:
    id: str
    title: str
    mood: str
    duration: float
    path: str
    builtin: bool
    source: str

    def to_dict(self) -> Dict[str, object]:
        return {
            "id": self.id,
            "title": self.title,
            "mood": self.mood,
            "duration": round(float(self.duration), 3),
            "path": self.path,
            "builtin": bool(self.builtin),
            "source": self.source,
        }


@dataclass(frozen=True)
class BuiltinSpec:
    """One procedurally generated track."""

    id: str
    title: str
    mood: str
    seed: int
    duration: float
    generator: str = ""


BUILTIN_SPECS: Tuple[BuiltinSpec, ...] = (
    BuiltinSpec("ambient-drift", "Ambient Drift", "ambient", 1013, 42.0, "_gen_ambient"),
    BuiltinSpec("tension-approach", "Tension Approach", "tension", 2027, 36.0, "_gen_tension"),
    BuiltinSpec("uplifting-ascent", "Uplifting Ascent", "uplifting", 3041, 32.0, "_gen_uplifting"),
    BuiltinSpec("dark-depths", "Dark Depths", "dark", 4051, 40.0, "_gen_dark"),
    BuiltinSpec("documentary-horizon", "Documentary Horizon", "documentary", 5059, 34.0, "_gen_documentary"),
    BuiltinSpec("lofi-nightfall", "Lofi Nightfall", "lofi", 6067, 34.0, "_gen_lofi"),
)

_BUILTIN_BY_ID = {spec.id: spec for spec in BUILTIN_SPECS}

# RNG used by pick_track. A dedicated instance rather than the global ``random``
# module so a caller (or a test) can inject a seeded one and keep the suite
# reproducible. In production this is left unseeded, so auto-BGM really does
# vary between renders instead of quietly repeating.
_music_rng = random.Random()


# ----------------------------------------------------------------- path safety

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_MOOD_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


def _is_safe_track_id(track_id: object) -> bool:
    """True when *track_id* is a bare filename stem that cannot escape MUSIC_DIR.

    Checked before any filesystem access. Rejects separators (posix and windows),
    "..", absolute paths, drive letters and NUL, so "../secrets", "a/b", "a\\b"
    and "C:\\Windows" are all refused.
    """
    if not isinstance(track_id, str):
        return False
    if not track_id or "\x00" in track_id:
        return False
    if track_id != track_id.strip():
        return False
    if ".." in track_id:
        return False
    if not _SAFE_ID_RE.match(track_id):
        return False
    if os.path.isabs(track_id):
        return False
    if re.match(r"^[A-Za-z]:", track_id):  # windows drive-relative, e.g. "C:evil"
        return False
    return len(track_id) <= 120


def _strip_accents(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _slugify(value: object, fallback: str = "track") -> str:
    """Lowercase, accent-free, separator-free slug.

    Every character that could be read as a path instruction ("/", "\\", ".", ":",
    NUL) is dropped, so a hostile title such as "../../etc/passwd" can only ever
    yield the harmless slug "etcpasswd" and can never escape MUSIC_DIR.
    """
    text = _strip_accents(str(value or "")).lower()
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"[^a-z0-9-]+", "", text)
    text = re.sub(r"-{2,}", "-", text).strip("-")
    return text[:80] or fallback


def _normalise_mood(mood: object) -> str:
    text = _slugify(mood, "").replace("-", "_")
    return text if _MOOD_RE.match(text) else DEFAULT_MOOD


# ------------------------------------------------------------------- manifests


def _manifest_path() -> Path:
    return MUSIC_DIR / MANIFEST_NAME


def _read_manifest() -> Dict[str, Dict[str, object]]:
    try:
        raw = json.loads(_manifest_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _write_manifest(entries: Dict[str, Dict[str, object]]) -> None:
    path = _manifest_path()
    try:
        MUSIC_DIR.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(MANIFEST_NAME + ".tmp")
        tmp.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


# ----------------------------------------------------------------- ffmpeg bits


def _probe_duration(path: Path) -> float:
    """Duration in seconds via ffprobe. 0.0 when ffprobe is missing or fails."""
    try:
        probe = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=nw=1:nk=1",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return float(str(probe.stdout).strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0.0


# ------------------------------------------------------------------- catalogue


def list_tracks(mood: str = "") -> List[Track]:
    """Every audio file in MUSIC_DIR, optionally filtered by mood.

    Never raises: an unreadable directory or a corrupt manifest yields whatever
    could be read, and [] when there is nothing at all.
    """
    tracks: List[Track] = []
    try:
        MUSIC_DIR.mkdir(parents=True, exist_ok=True)
        files = sorted(
            entry for entry in MUSIC_DIR.iterdir()
            if entry.is_file() and entry.suffix.lower() in AUDIO_SUFFIXES
        )
    except OSError:
        return []

    manifest = _read_manifest()
    wanted = _normalise_mood(mood) if mood else ""

    for path in files:
        try:
            spec = _BUILTIN_BY_ID.get(path.stem)
            if spec is not None:
                title, track_mood, duration, builtin = spec.title, spec.mood, spec.duration, True
            else:
                meta = manifest.get(path.stem) or {}
                builtin = False
                title = str(meta.get("title") or path.stem)
                track_mood = _normalise_mood(meta.get("mood") or DEFAULT_MOOD)
                try:
                    duration = float(meta.get("duration") or 0.0)
                except (TypeError, ValueError):
                    duration = 0.0
                if duration <= 0.0:
                    duration = _probe_duration(path)
            if wanted and track_mood != wanted:
                continue
            tracks.append(Track(
                id=path.stem,
                title=title,
                mood=track_mood,
                duration=duration,
                path=str(path),
                builtin=builtin,
                source="builtin" if builtin else "upload",
            ))
        except OSError:
            continue

    tracks.sort(key=lambda t: (
        0 if t.builtin else 1,
        MOODS.index(t.mood) if t.mood in MOODS else len(MOODS),
        t.id,
    ))
    return tracks


def get_track(track_id: str) -> Optional[Track]:
    """Look up one track. Unsafe ids are refused before touching the filesystem."""
    if not _is_safe_track_id(track_id):
        return None
    tracks = list_tracks()
    for track in tracks:
        if track.id == track_id:
            return track
    folded = track_id.lower()
    for track in tracks:
        if track.id.lower() == folded:
            return track
    return None


def search_tracks(query: str) -> List[Track]:
    """Case- and accent-insensitive substring match over title, mood and id."""
    needle = _strip_accents(str(query or "")).strip().lower()
    if not needle:
        return []
    hits = []
    for track in list_tracks():
        haystack = _strip_accents(f"{track.title} {track.mood} {track.id}").lower()
        if needle in haystack:
            hits.append(track)
    return hits


def register_upload(source_path: Path, title: str = "", mood: str = "ambient") -> Track:
    """Copy a user file into MUSIC_DIR and register it in the manifest.

    The stored name is ``upload-<title-slug>-<hash><ext>``; the content hash keeps
    two different songs with the same title apart and makes a re-upload of the
    same file idempotent. ``title`` is slugged, so a title containing "../" cannot
    escape MUSIC_DIR.

    Raises ValueError for an unsupported extension, a missing source file, or a
    file that cannot be read.
    """
    source = Path(source_path)
    suffix = source.suffix.lower()
    if suffix not in AUDIO_SUFFIXES:
        raise ValueError(f"Unsupported audio format: {suffix or source.name}")
    if not source.is_file():
        raise ValueError(f"Audio file not found: {source}")

    clean_title = str(title or "").strip() or source.stem
    slug = _slugify(clean_title, "custom-track")
    track_mood = _normalise_mood(mood)

    try:
        digest = hashlib.sha256(source.read_bytes()).hexdigest()[:10]
    except OSError as exc:
        raise ValueError(f"Could not read audio file: {source}") from exc

    target = MUSIC_DIR / f"upload-{slug}-{digest}{suffix}"
    if not _is_safe_track_id(target.stem):  # pragma: no cover - slugifier guarantees it
        raise ValueError("Could not build a safe file name for this upload")

    try:
        MUSIC_DIR.mkdir(parents=True, exist_ok=True)
        if source.resolve() != target.resolve():
            shutil.copyfile(source, target)
    except OSError as exc:
        raise ValueError(f"Could not store audio file: {source}") from exc

    duration = _probe_duration(target)
    track_id = target.stem
    entries = _read_manifest()
    entries[track_id] = {
        "title": clean_title,
        "mood": track_mood,
        "duration": duration,
        "source": source.name,
    }
    _write_manifest(entries)

    return Track(
        id=track_id,
        title=clean_title,
        mood=track_mood,
        duration=duration,
        path=str(target),
        builtin=False,
        source="upload",
    )


def pick_track(mood: str = "", exclude_ids: Optional[List[str]] = None) -> Optional[Track]:
    """Pick a track for auto-BGM.

    Materialises the builtin library first. ``storage/music/*`` is gitignored,
    so a fresh checkout ships an empty directory and the six procedural tracks
    only exist once something generates them. Auto-BGM is that something: the
    catalogue is on disk by the time a track is chosen. Idempotent, and one
    ``stat`` per spec once the library exists.

    Honours ``exclude_ids`` so a re-render does not repeat the track it just
    used, and falls back to the whole library when the mood has nothing left, so
    a render still gets music. None only when no track can be produced at all -
    an unbuildable library, or every candidate excluded - and never a fabricated
    id; callers should then report ``missing_library_note()``.

    Among the admissible candidates the choice is random, not first-match and
    not mood-deterministic: with several tracks in one mood a re-render of the
    same topic no longer lands on the same track by luck of ordering. The RNG is
    ``music._music_rng`` - inject a seeded ``random.Random`` to make a run
    reproducible. When a mood has exactly one candidate the choice is a no-op;
    that is the case for the six built-ins, so the benefit only shows up once
    someone uploads tracks.
    """
    ensure_builtin_library()
    excluded = {str(item) for item in (exclude_ids or [])}
    candidates = [t for t in list_tracks(mood) if t.id not in excluded]
    if not candidates and mood:
        candidates = [t for t in list_tracks() if t.id not in excluded]
    if not candidates:
        return None
    return _music_rng.choice(candidates)


# ------------------------------------------------------------------ generation


def _require_numpy():
    if np is None:  # pragma: no cover - only on a clone without numpy installed
        raise RuntimeError(
            "numpy is required to generate the built-in music library; "
            "install it with 'pip install -r requirements.txt'."
        )
    return np


def _timebase(duration: float):
    # float32 halves the work in every subsequent sin() without an audible cost;
    # the running-average helper promotes back to float64 because a float32
    # cumsum over millions of samples drifts.
    n = max(1, int(round(float(duration) * SAMPLE_RATE)))
    return (np.arange(n, dtype=np.float32) / SAMPLE_RATE).astype(np.float32)


def _moving_average(x, width: int):
    """Box filter, O(n). Odd widths only so the output keeps the input length."""
    width = int(max(1, width))
    if width % 2 == 0:
        width += 1
    if width <= 1 or width > x.size:
        return x
    pad = width // 2
    padded = np.pad(np.asarray(x, dtype=np.float64), pad, mode="edge")
    cumulative = np.concatenate(([0.0], np.cumsum(padded)))
    return (cumulative[width:] - cumulative[:-width])[:x.size] / float(width)


def _lowpass(x, cutoff: float, slope: float = 2.0):
    """Zero-phase FFT lowpass - a darkening filter without scipy."""
    x = np.asarray(x, dtype=np.float64)
    spectrum = np.fft.rfft(x)
    freqs = np.fft.rfftfreq(x.size, d=1.0 / SAMPLE_RATE)
    gain = 1.0 / (1.0 + (freqs / max(cutoff, 1.0)) ** (2.0 * slope))
    return np.fft.irfft(spectrum * gain, n=x.size)


def _detuned_pair(freq: float, t, rng, detune: float = 0.0015):
    """Two copies of a sine, slightly detuned and with independent phase."""
    left = np.sin(2 * np.pi * freq * t + rng.uniform(0.0, 2 * np.pi))
    right = np.sin(2 * np.pi * freq * (1.0 + detune) * t + rng.uniform(0.0, 2 * np.pi))
    return left, right


def _pluck(freq: float, t, decay: float = 3.2, partials=(1.0, 0.35, 0.14)):
    env = np.exp(-decay * t)
    out = np.zeros_like(t)
    for index, amp in enumerate(partials, start=1):
        out += amp * np.sin(2 * np.pi * freq * index * t)
    return out * env


def _gen_ambient(duration: float, rng):
    """Slow sine pads, detuned fifths, almost no dynamics."""
    t = _timebase(duration)
    left = np.zeros_like(t)
    right = np.zeros_like(t)
    for ratio, gain, drift in ((1.0, 0.55, 0.055), (1.5, 0.30, 0.041),
                               (2.0, 0.20, 0.033), (3.0, 0.10, 0.027)):
        a, b = _detuned_pair(110.0 * ratio, t, rng, 0.0018)
        lfo = 0.5 + 0.5 * np.sin(2 * np.pi * drift * t + rng.uniform(0.0, 2 * np.pi))
        left += gain * a * lfo
        right += gain * b * (1.0 - lfo)
    air = _lowpass(rng.standard_normal(t.size, dtype=np.float32) * 0.05, 1800.0)
    left = _lowpass(_moving_average(left, 2205) + air, 3200.0)
    right = _lowpass(_moving_average(right, 2205) - air, 3200.0)
    return np.column_stack((left, right))


def _gen_tension(duration: float, rng):
    """Low drone, slow tremolo, sparse high ticks."""
    t = _timebase(duration)
    left = np.zeros_like(t)
    right = np.zeros_like(t)
    for freq, gain, detune in ((55.0, 0.50, 0.0), (55.6, 0.34, 0.001),
                               (82.5, 0.22, 0.0), (110.0, 0.16, 0.0015)):
        a, b = _detuned_pair(freq, t, rng, detune)
        left += gain * a
        right += gain * b
    tremolo = 0.66 + 0.34 * np.sin(2 * np.pi * 0.27 * t + rng.uniform(0.0, 2 * np.pi))
    left *= tremolo
    right *= (1.32 - tremolo)
    swell = 0.5 + 0.5 * np.sin(2 * np.pi * 0.05 * t)
    noise = _lowpass(rng.standard_normal(t.size, dtype=np.float32) * 0.30, 420.0) * swell
    left += noise
    right += np.roll(noise, 617) * 0.9

    # Sparse metallic ticks: a decaying two-tone blip every few seconds.
    tick_len = max(1, int(0.08 * SAMPLE_RATE))
    tick_t = np.arange(tick_len) / SAMPLE_RATE
    tick = np.sin(2 * np.pi * 1870.0 * tick_t) + 0.6 * np.sin(2 * np.pi * 2790.0 * tick_t)
    tick *= np.exp(-38.0 * tick_t)
    step = 3.0
    start = 0.5
    while start < duration - 0.5:
        index = int(start * SAMPLE_RATE)
        if index + tick_len < t.size:
            pan = float(rng.uniform(0.25, 0.75))
            left[index:index + tick_len] += tick * pan * 0.5
            right[index:index + tick_len] += tick * (1.0 - pan) * 0.5
        start += step
        step = float(rng.uniform(3.0, 5.0))

    return np.column_stack((_lowpass(left, 6000.0), _lowpass(right, 6000.0)))


def _gen_uplifting(duration: float, rng):
    """Bright major arpeggio over a soft sustained chord."""
    t = _timebase(duration)
    left = np.zeros_like(t)
    right = np.zeros_like(t)
    for freq, gain in ((220.0, 0.22), (277.18, 0.18), (329.63, 0.20), (440.0, 0.10)):
        a, b = _detuned_pair(freq, t, rng, 0.0012)
        lfo = 0.6 + 0.4 * np.sin(2 * np.pi * 0.07 * t + rng.uniform(0.0, 2 * np.pi))
        left += gain * a * lfo
        right += gain * b * (1.0 - lfo)

    scale = (440.0, 554.37, 659.25, 554.37, 493.88, 659.25, 554.37, 440.0)
    step = 0.25
    note_len = int(step * SAMPLE_RATE)
    note_t = np.arange(note_len) / SAMPLE_RATE
    position = 0.0
    index = 0
    while position < duration:
        start = int(position * step * SAMPLE_RATE)
        if start + note_len >= t.size:
            break
        note = _pluck(scale[index % len(scale)], note_t, decay=3.0)
        pan = 0.5 + 0.35 * np.sin(2 * np.pi * 0.11 * position)
        left[start:start + note_len] += note * pan * 0.55
        right[start:start + note_len] += note * (1.0 - pan) * 0.55
        position += 1
        index += 1
    return np.column_stack((_lowpass(left, 9000.0), _lowpass(right, 9000.0)))


def _gen_dark(duration: float, rng):
    """Low minor drone under a heavy lowpass."""
    t = _timebase(duration)
    left = np.zeros_like(t)
    right = np.zeros_like(t)
    for freq, gain in ((55.0, 0.55), (65.41, 0.34), (82.41, 0.24), (110.0, 0.12)):
        a, b = _detuned_pair(freq, t, rng, 0.0022)
        left += gain * a
        right += gain * b
    breathe = 0.72 + 0.28 * np.sin(2 * np.pi * 0.037 * t + rng.uniform(0.0, 2 * np.pi))
    rumble = _lowpass(rng.standard_normal(t.size, dtype=np.float32) * 0.45, 110.0)
    left = _lowpass(left * breathe + rumble, 850.0, slope=3.0)
    right = _lowpass(right * (1.44 - breathe) + np.roll(rumble, 1301), 850.0, slope=3.0)
    return np.column_stack((left, right))


def _gen_documentary(duration: float, rng):
    """Neutral and restrained: slow mid-range chords over a silent noise floor."""
    t = _timebase(duration)
    left = np.zeros_like(t)
    right = np.zeros_like(t)
    chords = (
        (196.00, 246.94, 293.66),
        (220.00, 261.63, 329.63),
        (174.61, 220.00, 261.63),
        (196.00, 246.94, 392.00),
    )
    span = max(6.0, duration / len(chords))
    index = 0
    while index * span < duration:
        chord = chords[index % len(chords)]
        start = index * span
        length = min(span * 1.5, duration - start)
        if length <= 0:
            break
        window_t = np.arange(int(length * SAMPLE_RATE)) / SAMPLE_RATE
        edge = max(span * 0.35, 0.1)
        env = np.clip(
            np.minimum(window_t / edge, (length - window_t) / edge), 0.0, 1.0
        )
        env *= 0.85 + 0.15 * np.sin(2 * np.pi * 0.09 * window_t)
        offset = int(start * SAMPLE_RATE)
        available = min(window_t.size, t.size - offset)
        for freq in chord:
            voice = np.zeros_like(window_t)
            for harmonic in range(1, 7):
                voice += np.sin(2 * np.pi * freq * harmonic * window_t) / (harmonic ** 1.6)
            wide = np.roll(voice, int(0.004 * SAMPLE_RATE))
            if available > 0:
                left[offset:offset + available] += voice[:available] * env[:available] * 0.16
                right[offset:offset + available] += wide[:available] * env[:available] * 0.16
        index += 1
    floor = _lowpass(rng.standard_normal(t.size, dtype=np.float32) * 0.04, 1200.0)
    return np.column_stack((left + floor, right - floor))


def _gen_lofi(duration: float, rng):
    """Warm chord bed with wow/flutter, soft kick, noise snare, vinyl crackle."""
    t = _timebase(duration)
    wow = 1.0 + 0.0035 * np.sin(2 * np.pi * 0.6 * t) + 0.0015 * np.sin(2 * np.pi * 3.1 * t)
    left = np.zeros_like(t)
    right = np.zeros_like(t)
    for freq, gain in ((174.61, 0.30), (220.00, 0.24), (261.63, 0.22), (329.63, 0.14)):
        left += gain * np.sin(2 * np.pi * freq * wow * t + rng.uniform(0.0, 2 * np.pi))
        right += gain * np.sin(2 * np.pi * freq * (1.0 - 0.002) * t + rng.uniform(0.0, 2 * np.pi))

    kick_len = int(0.28 * SAMPLE_RATE)
    kick_t = np.arange(kick_len) / SAMPLE_RATE
    kick = np.sin(2 * np.pi * (52.0 * kick_t - 1.6 * kick_t ** 2)) * np.exp(-11.0 * kick_t)
    snare_len = int(0.22 * SAMPLE_RATE)
    snare_t = np.arange(snare_len) / SAMPLE_RATE
    snare = _lowpass(rng.standard_normal(snare_len, dtype=np.float32) * np.exp(-16.0 * snare_t), 2400.0) * 0.28

    step_index = 0
    position = 0.0
    while position < duration:
        start = int(position * SAMPLE_RATE)
        if start + kick_len < t.size:
            left[start:start + kick_len] += kick * 0.25
            right[start:start + kick_len] += kick * 0.25
        if step_index % 4 in (1, 3) and start + snare_len < t.size:
            left[start:start + snare_len] += snare * 0.5
            right[start:start + snare_len] += snare * 0.5
        position += 0.5
        step_index += 1

    crackle_len = int(0.012 * SAMPLE_RATE)
    for _ in range(int(duration * 18)):
        start = int(rng.uniform(0.0, max(0.0, duration - 0.02)) * SAMPLE_RATE)
        if start + crackle_len >= t.size:
            break
        crackle = rng.standard_normal(crackle_len, dtype=np.float32) * float(rng.uniform(0.05, 0.16))
        left[start:start + crackle_len] += _lowpass(crackle, 6000.0)
        right[start:start + crackle_len] += _lowpass(crackle, 5500.0)

    return np.column_stack((_lowpass(left, 8000.0), _lowpass(right, 8000.0)))


_GENERATORS = {
    "_gen_ambient": _gen_ambient,
    "_gen_tension": _gen_tension,
    "_gen_uplifting": _gen_uplifting,
    "_gen_dark": _gen_dark,
    "_gen_documentary": _gen_documentary,
    "_gen_lofi": _gen_lofi,
}


def _finalise(samples):
    """Fade the edges and peak-normalise to -3 dBFS. Returns an (n, 2) array."""
    array = np.asarray(samples, dtype=np.float64)
    if array.ndim == 1:
        array = np.column_stack((array, array))
    n = array.shape[0]
    fade_in = int(min(FADE_IN, n / (4.0 * SAMPLE_RATE)) * SAMPLE_RATE)
    fade_out = int(min(FADE_OUT, n / (4.0 * SAMPLE_RATE)) * SAMPLE_RATE)
    if fade_in > 1:
        array[:fade_in] *= np.linspace(0.0, 1.0, fade_in)[:, None]
    if fade_out > 1:
        array[n - fade_out:] *= np.linspace(1.0, 0.0, fade_out)[:, None]
    peak = float(np.max(np.abs(array))) if array.size else 0.0
    if peak > 0.0:
        array *= TARGET_PEAK / peak
    return array


def _write_wav(path: Path, samples) -> Path:
    array = np.asarray(samples, dtype=np.float64)
    if array.ndim == 1:
        array = np.column_stack((array, array))
    pcm = np.clip(np.round(array * 32767.0), -32768, 32767).astype("<i2")
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(pcm.tobytes())
    return path


def synthesize(spec: BuiltinSpec, output_path: Path, duration: Optional[float] = None) -> Path:
    """Render one builtin spec to a 44.1kHz stereo WAV. Deterministic per spec."""
    _require_numpy()
    generator = _GENERATORS[spec.generator]
    rng = np.random.default_rng(spec.seed)
    samples = generator(float(duration or spec.duration), rng)
    return _write_wav(Path(output_path), _finalise(samples))


def generate_builtin_track(track_id: str, output_path: Optional[Path] = None,
                           duration: Optional[float] = None) -> Path:
    """Render a single builtin track; ``duration`` overrides the spec length."""
    spec = _BUILTIN_BY_ID.get(track_id)
    if spec is None:
        raise ValueError(f"Unknown builtin track: {track_id}")
    target = Path(output_path) if output_path is not None else MUSIC_DIR / f"{spec.id}.wav"
    return synthesize(spec, target, duration=duration)


def ensure_builtin_library() -> List[Track]:
    """Generate any missing builtin track, then return the whole catalogue.

    Idempotent: a track whose WAV already exists is never regenerated, so a
    server restart costs nothing. Safe to call on every start; a failure on one
    track is swallowed and the rest of the library still appears.
    """
    try:
        MUSIC_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        return list_tracks()

    for spec in BUILTIN_SPECS:
        target = MUSIC_DIR / f"{spec.id}.wav"
        try:
            if target.exists() and target.stat().st_size > 0:
                continue
        except OSError:
            continue
        try:
            synthesize(spec, target)
        except Exception:
            continue
    return list_tracks()


def missing_library_note() -> str:
    """Portuguese explanation for the one case auto-BGM cannot cover.

    ``pick_track`` returns None when no track can be produced at all, and the
    render then reports ``applied=False``. With an empty note the user would get
    a silent video and no explanation, so the wording for that failure lives
    here, next to the library that failed to appear. Never raises and never
    claims a track exists.
    """
    if np is None:
        return (
            "musica nao aplicada: a biblioteca de trilhas esta vazia e o numpy "
            "nao esta instalado, portanto a biblioteca integrada nao pode ser "
            "gerada; o video foi mantido sem trilha sonora."
        )
    return (
        "musica nao aplicada: nenhuma faixa da biblioteca de trilhas ficou "
        "disponivel e a biblioteca integrada nao pode ser gerada; o video foi "
        "mantido sem trilha sonora."
    )


# --------------------------------------------------------------------- mixing


def mix_parameters(music_volume: float = DEFAULT_MUSIC_VOLUME,
                   duck_voice: bool = True) -> Dict[str, object]:
    """Mixer kwargs for the render engine.

    ``music_volume`` is clamped to 0.0..1.0. Music sits 12 dB under the voice
    target, is lowpassed at 8 kHz (it is a bed, not a lead) and fades in/out so it
    never starts or stops abruptly.
    """
    try:
        volume = float(music_volume)
    except (TypeError, ValueError):
        volume = DEFAULT_MUSIC_VOLUME
    if volume != volume:  # NaN
        volume = DEFAULT_MUSIC_VOLUME
    volume = min(1.0, max(0.0, volume))
    return {
        "music_volume": volume,
        "duck_voice": bool(duck_voice),
        "voice_target_db": VOICE_TARGET_DB,
        "music_target_db": MUSIC_TARGET_DB,
        "fade_in": FADE_IN,
        "fade_out": FADE_OUT,
        "filter": MIX_FILTER,
    }


def _copy_audio(track_path: Path, output_path: Path) -> Path:
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if track_path.exists() and track_path.resolve() != output_path.resolve():
            shutil.copyfile(track_path, output_path)
            return output_path
    except OSError:
        pass
    return output_path if output_path.exists() else track_path


def loop_to_duration(track_path: Path, duration: float, output_path: Path) -> Path:
    """Make the track cover the whole video, with a fade in and out.

    One ffmpeg call (``-stream_loop -1``) when the track is shorter than the
    video. Degrades to a plain copy when ffmpeg is missing or the track is
    already long enough. Never raises: the returned Path is the best usable
    audio (the copy when available, otherwise the source).
    """
    source = Path(track_path)
    target = Path(output_path)
    try:
        wanted = max(0.0, float(duration))
    except (TypeError, ValueError):
        wanted = 0.0

    source_duration = _probe_duration(source)
    needs_loop = wanted > 0.0 and wanted > source_duration + 0.05

    if needs_loop:
        fade_in = min(FADE_IN, wanted * 0.4)
        fade_out = min(FADE_OUT, wanted * 0.4)
        fade_start = max(0.0, wanted - fade_out)
        filters = (
            f"afade=t=in:st=0:d={fade_in:.3f},"
            f"afade=t=out:st={fade_start:.3f}:d={fade_out:.3f}"
        )
        command = [
            "ffmpeg", "-v", "error", "-y",
            "-stream_loop", "-1",
            "-i", str(source),
            "-t", f"{wanted:.3f}",
            "-af", filters,
            "-ac", "2",
            "-ar", str(SAMPLE_RATE),
            "-c:a", "pcm_s16le",
            str(target),
        ]
        try:
            subprocess.run(command, check=True, capture_output=True, text=True, timeout=600)
            if target.exists() and target.stat().st_size > 0:
                return target
        except Exception:
            pass

    return _copy_audio(source, target)


if __name__ == "__main__":
    import sys

    if "--mix" in sys.argv:
        raw = sys.argv[sys.argv.index("--mix") + 1] if len(sys.argv) > sys.argv.index("--mix") + 1 else "0.5"
        print(json.dumps(mix_parameters(float(raw)), ensure_ascii=False))
    else:
        print(json.dumps([t.to_dict() for t in ensure_builtin_library()], ensure_ascii=False, indent=2))
