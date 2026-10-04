"""One-click topic -> finished video orchestrator + in-process job registry.

This module is the headline feature of Dark Studio: give it a topic and it runs
the whole pipeline (script -> voice -> media -> render) in a background asyncio
task, tracking progress in a bounded, concurrency-safe registry.

It deliberately does NOT import backend.app or any HTTP layer: it only talks to
the service modules, so it stays importable standalone (``python3 -c
"import backend.services.generator"``) and can be unit-tested without a server.

Concurrency
    At most MAX_CONCURRENT_JOBS jobs may be RUNNING at once. Extra submissions
    queue behind a module-level asyncio.Semaphore and wait for a slot; a worker
    never blocks the caller - submit_job returns immediately with a QUEUED job.

Never block the loop
    Every blocking call the worker makes runs in a thread. Synchronous services
    (script generation over HTTP, stock-media search, ffprobe) go through
    ``asyncio.to_thread``; the coroutine services whose bodies still contain
    blocking work (TTS, render) go through ``_off_loop``, which drives them on a
    private event loop inside a worker thread. The loop therefore stays free for
    the whole life of a job, so ``GET /api/jobs/{id}`` - the endpoint the whole
    WebUI polls - keeps answering and ``POST /api/generate`` really does return
    immediately. A single blocking call left on the loop is enough to freeze the
    UI for the seconds it lasts.

Unique outputs
    Every job writes to its own stem: the topic slug suffixed with the job id, so
    two jobs on the same topic can never clobber each other's MP4. An explicit
    ``project_prefix`` stays authoritative, because then the caller has named the
    file on purpose.

Captions come from the text
    The generator already holds ``script.full_text`` - the exact string the TTS
    read - so the SRT is derived from that text and never from transcribing the
    audio back. Speech recognition there costs a model load plus a full decode to
    recover a lossy copy of text already in hand, and when faster-whisper is
    missing it silently invents five sentences about routine and exercise.

Cancellation
    There is no shared kill switch. cancel_job only prevents a *not-yet-started*
    job (status QUEUED) from running; a RUNNING job runs to completion and
    cannot be cancelled. That is a documented limitation, not a hidden one:
    cancelling mid-render would leave a half-written MP4 on disk.

Degrade, never crash
    Any stage failure is caught, recorded on the job as state=FAILED with a
    Portuguese message, and the exception is swallowed. A failed job never blocks
    the queue and never propagates to the caller.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from backend.services import music, pipeline, render_engine, script_gen, style, tts

# --------------------------------------------------------------------------- constants

# How many renders may run headless-Chrome simultaneously. Five at once would
# exhaust memory on a normal machine; two is a safe default for a dev box.
MAX_CONCURRENT_JOBS: int = 2

# Bounded history: finished jobs older than this are pruned on every mutation so
# a long-running server does not leak one Job per completed render.
MAX_JOB_HISTORY: int = 200

# Progress checkpoints. The values are part of the public contract (the UI polls
# them), so they live here as named constants rather than magic literals.
PROGRESS_SCRIPT: float = 0.05
PROGRESS_VOICE: float = 0.20
PROGRESS_MEDIA: float = 0.40
PROGRESS_RENDER: float = 0.60
PROGRESS_DONE: float = 1.00

# Default aspect ratios the renderer understands.
VALID_ASPECT_RATIOS = ("vertical", "square", "landscape")

# Default GenerationRequest-style params when the caller omits optional keys.
DEFAULT_LANGUAGE = "pt-PT"
DEFAULT_VOICE = tts.DEFAULT_VOICE
DEFAULT_TONE = "documentary"
DEFAULT_PRESET = "cinematic"
DEFAULT_MUSIC_MOOD = "ambient"
DEFAULT_MUSIC_VOLUME = music.DEFAULT_MUSIC_VOLUME
DEFAULT_SECTION_COUNT = 5
DEFAULT_DURATION_TARGET = 60
DEFAULT_DUCK_VOICE = True
DEFAULT_TTS_PROVIDER = "edge"
DEFAULT_RATE = "+0%"

# Time source. Module-level so tests can monkeypatch generator._time = lambda: 0.0
# and make the registry deterministic.
_time = time.time

# How much of the job id goes into the default output stem. Eight hex characters
# (32 bits) keeps the filename recognisable at a glance while staying unique
# across jobs; a collision is additionally impossible because _dedupe_output_stem
# refuses a stem whose file is already on disk.
OUTPUT_TOKEN_LEN = 8

# Bound on the "-2, -3, ..." search when the stem is already taken on disk. Past
# this many attempts something else is generating names faster than we can probe,
# so the loop gives up and appends a fresh uuid instead of spinning.
OUTPUT_DEDUPE_LIMIT = 50

# Caption splitting used by the local fallback. Same numbers the pipeline helper
# uses (two comfortable lines, and a floor so a cue is never zero-length).
DEFAULT_CAPTION_CHARS = 84
MIN_CAPTION_SECONDS = 0.2

# How far the delivered video may sit from duration_target before the job says
# so out loud: 10% of the target, never less than 1.5 s, because sub-second
# ffprobe noise is not a missed target.
DURATION_TARGET_REL_TOLERANCE = 0.10
DURATION_TARGET_ABS_TOLERANCE = 1.5

# Sentence boundaries a caption may be split after, kept conservative so an
# abbreviation like "Sr." does not become a cue of its own.
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?\u2026])\s+")


# --------------------------------------------------------------------------- slugify

def _project_slug(params: dict) -> str:
    """Base, human-readable name for a job.

    Uses ``project_prefix`` when the caller supplied one (so a batch of variants
    can be told apart), otherwise slugs the topic. The result is guaranteed
    path-safe, so output_stem and create_project_dir can never escape the storage
    directory.

    This is only the *base*: it is derived from the request alone, so two jobs
    asking for the same topic share it. _output_stem is what makes the stem unique.
    """
    prefix = (params.get("project_prefix") or "").strip()
    if prefix:
        base = slugify_topic(prefix, max_len=40)
    else:
        base = slugify_topic(params.get("topic", ""), max_len=40)
    return base or "projeto"


def _safe_token(value: str) -> str:
    """Reduce a job id to the [a-z0-9] characters a filename may carry."""
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())[:OUTPUT_TOKEN_LEN]


def _dedupe_output_stem(stem: str, token: str, output_dir: Path) -> str:
    """Return ``stem``, or ``stem-2``/``stem-3``... if that file already exists.

    The job id already makes the stem unique among jobs in this registry; this is
    the belt to that pair of braces, so a name can never be reused because
    something else on disk already owns it (a previous run of the server, a
    manually re-run job, a caller that reused its own prefix earlier).
    """
    candidate = stem
    attempt = 2
    while (output_dir / f"{candidate}.mp4").exists() or (output_dir / f"{candidate}.json").exists():
        if attempt > OUTPUT_DEDUPE_LIMIT:
            return f"{stem}-{uuid.uuid4().hex[:4]}"
        candidate = f"{stem}-{attempt}"
        attempt += 1
    return candidate


def _output_stem(params: dict, job_id: str, output_dir: Path) -> str:
    """The stem every file of this job is written under.

    Default: ``<topic-slug>-<job-id>``. The slug alone is NOT unique - two
    concurrent jobs on the same topic resolved to the same stem and the second
    render silently overwrote the first MP4 - so the job id is always part of it
    by default, and _dedupe_output_stem catches the case where even that name is
    taken. The topic slug stays at the front, so the filename is still readable.

    A caller-supplied ``project_prefix`` wins outright: the caller named the file,
    so a collision there is the caller's choice and only gets reported.
    """
    base = _project_slug(params)
    if (params.get("project_prefix") or "").strip():
        return base
    token = _safe_token(job_id) or uuid.uuid4().hex[:OUTPUT_TOKEN_LEN]
    return _dedupe_output_stem(f"{base}-{token}", token, output_dir)


# ------------------------------------------------------------ off-loop execution

def _drive_coroutine(make_coro: Callable[[], Any]) -> Any:
    """Run a service coroutine to completion on a private loop.

    Lives in a worker thread (see _off_loop). asyncio.run only installs signal
    handlers on the main thread, so this is safe to call from one.
    """
    return asyncio.run(make_coro())


async def _off_loop(make_coro: Callable[[], Any]) -> Any:
    """Await a coroutine service without parking the server loop inside it.

    ``tts.synthesize_speech_long`` and ``render_engine.render_video_hyperframes``
    are coroutines, but their bodies are not free of blocking work: the openai and
    azure TTS paths call ``httpx.post`` synchronously, and the render path runs
    ffprobe/ffmpeg through ``subprocess.run``, downloads assets with the sync
    httpx client and probes every image for a padded canvas. Awaiting those on the
    server loop parks the loop inside that blocking code, which is exactly the
    stall this module exists to avoid: ``GET /api/jobs/{id}`` stops answering
    until the call returns.

    The coroutine factory (not the coroutine) is handed over, so the coroutine is
    created inside the thread that runs it and never inherits loop-bound state
    from the server loop.
    """
    return await asyncio.to_thread(_drive_coroutine, make_coro)


def slugify_topic(topic: str, max_len: int = 40) -> str:
    """Turn a user topic into a filesystem-safe project stem.

    Lowercases, strips accents, replaces runs of spaces/specials with a single
    hyphen, collapses repeated hyphens and truncates. The result is guaranteed
    free of path separators and of "..", so a topic like "crise /2024" or an
    emoji-only string can never escape the project directory.
    """
    if not isinstance(topic, str):
        topic = str(topic or "")
    # NFKD decomposes accented letters into base + combining mark; dropping the
    # combining marks turns "café" -> "cafe" and "São" -> "sao" instead of
    # deleting the whole character (which is what encode("ascii", "ignore") does).
    import unicodedata
    decomposed = unicodedata.normalize("NFKD", topic)
    text = "".join(ch for ch in decomposed if not unicodedata.combining(ch)).lower()
    # Anything that is not a letter or digit becomes a hyphen; runs collapse.
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = re.sub(r"-{2,}", "-", text).strip("-")
    if not text:
        # Emoji-only or punctuation-only input still needs a usable stem.
        text = "projeto"
    return text[:max_len].strip("-") or "projeto"


# --------------------------------------------------------------------------- request

@dataclass
class GenerationRequest:
    """Validated parameters for one generation. ``validate()`` mutates nothing.

    The dataclass stores raw input; ``validate()`` returns a *copy* with defaults
    filled in and bounds checked, raising ValueError with a pt-PT message on the
    first problem found. Callers should call validate() before scheduling.
    """

    topic: str = ""
    language: str = DEFAULT_LANGUAGE
    voice: str = DEFAULT_VOICE
    tts_provider: str = DEFAULT_TTS_PROVIDER
    rate: str = DEFAULT_RATE
    section_count: int = DEFAULT_SECTION_COUNT
    tone: str = DEFAULT_TONE
    duration_target: int = DEFAULT_DURATION_TARGET
    custom_instructions: str = ""
    aspect_ratio: str = "vertical"
    preset: str = DEFAULT_PRESET
    subtitle_style: Optional[dict] = None
    music_track: Optional[str] = None
    music_mood: str = DEFAULT_MUSIC_MOOD
    music_volume: float = DEFAULT_MUSIC_VOLUME
    duck_voice: bool = DEFAULT_DUCK_VOICE
    include_captions: bool = True
    project_prefix: str = ""

    def validate(self) -> "GenerationRequest":
        """Return a copy with defaults applied and every field bounds-checked.

        Raises ValueError (pt-PT) on the first problem. Never mutates self.
        """
        topic = (self.topic or "").strip()
        if not topic:
            raise ValueError("Indique um tema para gerar o video.")

        language = (self.language or DEFAULT_LANGUAGE).strip() or DEFAULT_LANGUAGE
        if language not in script_gen.LANGUAGES:
            raise ValueError(
                f"Idioma '{language}' não é suportado. Escolha um de "
                f"{', '.join(script_gen.LANGUAGES)}."
            )

        try:
            section_count = int(self.section_count)
        except (TypeError, ValueError) as exc:
            raise ValueError("O número de secções tem de ser um inteiro.") from exc
        if not 1 <= section_count <= script_gen.MAX_SECTIONS:
            raise ValueError(
                f"O número de secções tem de estar entre 1 e "
                f"{script_gen.MAX_SECTIONS}."
            )

        try:
            duration_target = int(self.duration_target)
        except (TypeError, ValueError) as exc:
            raise ValueError("A duração-alvo tem de ser um número de segundos.") from exc
        if duration_target < 1:
            raise ValueError("A duração-alvo tem de ser maior que zero.")

        aspect_ratio = (self.aspect_ratio or "vertical").strip().lower()
        if aspect_ratio not in VALID_ASPECT_RATIOS:
            raise ValueError(
                f"Proporção '{aspect_ratio}' inválida. Use "
                f"{', '.join(VALID_ASPECT_RATIOS)}."
            )

        preset_name = (self.preset or DEFAULT_PRESET).strip().lower()
        valid_preset_names = {p.name for p in style.list_presets()}
        if preset_name not in valid_preset_names:
            raise ValueError(
                f"Preset '{preset_name}' inválido. Escolha um de "
                f"{', '.join(sorted(valid_preset_names))}."
            )

        try:
            music_volume = float(self.music_volume)
        except (TypeError, ValueError) as exc:
            raise ValueError("O volume da música tem de ser um número.") from exc
        if music_volume != music_volume or not 0.0 <= music_volume <= 1.0:
            raise ValueError("O volume da música tem de estar entre 0 e 1.")

        return GenerationRequest(
            topic=topic,
            language=language,
            voice=(self.voice or DEFAULT_VOICE).strip() or DEFAULT_VOICE,
            tts_provider=(self.tts_provider or DEFAULT_TTS_PROVIDER).strip().lower()
            or DEFAULT_TTS_PROVIDER,
            rate=(self.rate or DEFAULT_RATE).strip() or DEFAULT_RATE,
            section_count=section_count,
            tone=(self.tone or DEFAULT_TONE).strip() or DEFAULT_TONE,
            duration_target=duration_target,
            custom_instructions=(self.custom_instructions or "").strip(),
            aspect_ratio=aspect_ratio,
            preset=preset_name,
            subtitle_style=dict(self.subtitle_style) if self.subtitle_style else None,
            music_track=(self.music_track or "").strip() or None,
            music_mood=(self.music_mood or DEFAULT_MUSIC_MOOD).strip().lower()
            or DEFAULT_MUSIC_MOOD,
            music_volume=music_volume,
            duck_voice=bool(self.duck_voice),
            include_captions=bool(self.include_captions),
            project_prefix=(self.project_prefix or "").strip(),
        )


# --------------------------------------------------------------------------- job

@dataclass
class Job:
    """One generation request and its live state.

    ``params`` is the validated GenerationRequest.to_dict() the pipeline will
    consume; ``result`` is only populated on COMPLETED. Everything that moves
    (state, stage, progress, message) is updated in place by the pipeline, and
    the registry serialises a snapshot on every mutation so readers never see a
    half-written dict.
    """

    job_id: str
    status: str = "queued"
    progress: float = 0.0
    stage: str = "script"
    message: str = "Na fila."
    params: dict = field(default_factory=dict)
    result: Optional[dict] = None
    error: Optional[str] = None
    created_at: str = ""
    updated_at: str = ""
    # Monotonic insertion order. ISO-8601 timestamps only have second granularity,
    # so two jobs submitted in the same second would tie on updated_at; this
    # counter is the stable tiebreaker that keeps list_jobs() newest-first even
    # when the wall clock has not ticked.
    _seq: int = field(default=0, compare=False, repr=False)
    # Append-only log of every checkpoint the worker published, with its timestamp.
    # Not part of to_dict() on purpose: that dict is the public job contract and the
    # API layer compares it key-for-key. It rides along on the in-process copy (so
    # tests and /api handlers can read it) and lands in the result payload, which
    # is what makes a stage that finished faster than the first poll still visible
    # instead of silently skipped.
    stage_trail: List[Dict[str, Any]] = field(default_factory=list, compare=False, repr=False)

    def to_dict(self) -> dict:
        """JSON-ready snapshot. Deep-copies mutable fields so a caller cannot
        mutate the registry through the returned dict."""
        return {
            "job_id": self.job_id,
            "status": self.status,
            "progress": self.progress,
            "stage": self.stage,
            "message": self.message,
            "params": json.loads(json.dumps(self.params, default=str)),
            "result": json.loads(json.dumps(self.result, default=str)) if self.result else None,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "_seq": self._seq,
        }


# ----------------------------------------------------------------- registry state

# Module-level registry. All mutations happen under _lock; readers may observe a
# slightly stale snapshot, which is fine because every public read returns a
# deep copy via Job.to_dict().
_jobs: Dict[str, Job] = {}
_lock = asyncio.Lock()
# Bounded semaphore: acquiring a slot means "this job is RUNNING". Jobs that fail
# to acquire block here until a sibling finishes. Reused across event loops
# (pytest-asyncio spins one up per test), so it is created at module scope.
_slot_semaphore = asyncio.Semaphore(MAX_CONCURRENT_JOBS)
# Set of job ids currently inside the semaphore (i.e. RUNNING). Tracked so
# cancel_job can refuse to cancel a job that already started.
_running_ids: set = set()
# Monotonic insertion counter; see Job._seq.
_next_seq: int = 0


def _now_iso() -> str:
    """Current time as an ISO-8601 string (UTC, offset-less).

    Reads through ``_time`` so tests can monkeypatch the clock and make the
    registry deterministic (e.g. to force the prune window to zero).
    """
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(_time()))


def _touch(job: Job) -> None:
    job.updated_at = _now_iso()


def _prune_locked() -> None:
    """Drop finished jobs older than MAX_JOB_HISTORY.

    Only COMPLETED/FAILED/CANCELLED jobs are eligible - a job that is still
    queued or running must never be pruned, or the server would lose work.
    """
    cutoff = _time() - 3600.0  # one hour of history per slot; bounded below
    stale = [
        jid
        for jid, job in _jobs.items()
        if job.status in ("completed", "failed", "cancelled")
        and job.updated_at
        and _parse_iso(job.updated_at) < cutoff
    ]
    for jid in stale:
        _jobs.pop(jid, None)
    # Hard cap regardless of age: keep only the newest MAX_JOB_HISTORY entries.
    if len(_jobs) > MAX_JOB_HISTORY:
        ordered = sorted(
            _jobs.items(),
            key=lambda item: _parse_iso(item[1].updated_at) if item[1].updated_at else 0.0,
        )
        for jid, _job in ordered[: len(_jobs) - MAX_JOB_HISTORY]:
            _jobs.pop(jid, None)


def _parse_iso(value: str) -> float:
    """Parse an ISO-8601 timestamp back to epoch seconds. 0.0 on failure."""
    try:
        return time.mktime(time.strptime(value, "%Y-%m-%dT%H:%M:%SZ"))
    except (TypeError, ValueError):
        return 0.0


# --------------------------------------------------------------- public reads

def get_job(job_id: str) -> Optional[Job]:
    """Return a deep copy of the job, or None when it does not exist."""
    job = _jobs.get(job_id)
    return None if job is None else _copy_job(job)


def list_jobs() -> List[Job]:
    """Every job, newest first (by updated_at). Returns deep copies."""
    ordered = sorted(
        _jobs.values(),
        key=lambda job: (_parse_iso(job.updated_at) if job.updated_at else 0.0, job._seq),
        reverse=True,
    )
    return [_copy_job(job) for job in ordered]


def _copy_job(job: Job) -> Job:
    """Build an independent Job from a registry entry so callers cannot mutate
    shared state through the returned object."""
    data = job.to_dict()
    return Job(
        job_id=data["job_id"],
        status=data["status"],
        progress=data["progress"],
        stage=data["stage"],
        message=data["message"],
        params=data["params"],
        result=data["result"],
        error=data["error"],
        created_at=data["created_at"],
        updated_at=data["updated_at"],
        _seq=data.get("_seq", 0),
        stage_trail=[dict(entry) for entry in job.stage_trail],
    )


def cancel_job(job_id: str) -> bool:
    """Cooperative cancellation.

    Returns True when the job was still QUEUED and is now CANCELLED (it will
    never start). Returns False when the job does not exist, is RUNNING (a
    running job cannot be cancelled - it would leave a half-written MP4), or has
    already finished (COMPLETED/FAILED/CANCELLED). The caller can retry after the
    job settles.
    """
    job = _jobs.get(job_id)
    if job is None:
        return False
    if job.status != "queued":
        return False
    job.status = "cancelled"
    job.stage = "script"
    job.progress = 0.0
    job.message = "Cancelado pelo utilizador antes de começar."
    job.error = "Cancelado pelo utilizador."
    _touch(job)
    return True


# ------------------------------------------------------------ submit / schedule

def submit_job(params: dict) -> Job:
    """Validate params, register a QUEUED job and schedule it.

    Returns immediately with the job in QUEUED state. Raises ValueError only for
    an unvalidatable request (blank topic, bad preset, ...); every other failure
    is swallowed and turned into a FAILED job so the caller never sees an
    exception from the pipeline itself.
    """
    try:
        request = GenerationRequest(**(params or {})).validate()
    except ValueError:
        raise
    except Exception as exc:  # defensive: a stray kwarg must not 500 the caller
        raise ValueError(f"Parâmetros inválidos: {exc}") from exc

    global _next_seq
    _next_seq += 1
    job_id = uuid.uuid4().hex[:12]
    job = Job(
        _seq=_next_seq,
        job_id=job_id,
        status="queued",
        progress=0.0,
        stage="script",
        message="Na fila, à espera de uma slot livre.",
        params=_request_to_dict(request),
        created_at=_now_iso(),
        updated_at=_now_iso(),
    )
    _jobs[job_id] = job
    # Schedule the pipeline as a fire-and-forget task. The event loop is the
    # single owner of concurrency; the semaphore caps RUNNING jobs. When called
    # outside a running loop (e.g. from a test or a sync script) we simply leave
    # the job QUEUED - the caller is then responsible for driving _run_job.
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_run_job(job_id))
    except RuntimeError:
        pass
    return _copy_job(job)


def _request_to_dict(request: GenerationRequest) -> dict:
    """Serialise a validated GenerationRequest for the registry.

    Kept separate from a dataclasses.asdict call so the shape is stable and
    JSON-safe regardless of how the dataclass evolves.
    """
    return {
        "topic": request.topic,
        "language": request.language,
        "voice": request.voice,
        "tts_provider": request.tts_provider,
        "rate": request.rate,
        "section_count": request.section_count,
        "tone": request.tone,
        "duration_target": request.duration_target,
        "custom_instructions": request.custom_instructions,
        "aspect_ratio": request.aspect_ratio,
        "preset": request.preset,
        "subtitle_style": request.subtitle_style,
        "music_track": request.music_track,
        "music_mood": request.music_mood,
        "music_volume": request.music_volume,
        "duck_voice": request.duck_voice,
        "include_captions": request.include_captions,
        "project_prefix": request.project_prefix,
    }


# ------------------------------------------------------------- pipeline driver

async def _run_job(job_id: str) -> None:
    """Worker coroutine. Acquires a slot, runs the pipeline, releases it.

    The semaphore acquisition is the ONLY place a queued job can be delayed;
    once it holds a slot it runs to completion (or fails) so the queue drains.
    """
    job = _jobs.get(job_id)
    if job is None or job.status != "queued":
        return
    async with _slot_semaphore:
        # Re-read: cancel_job may have flipped us to cancelled while we waited.
        job = _jobs.get(job_id)
        if job is None or job.status != "queued":
            return
        _running_ids.add(job_id)
        job.status = "running"
        _stage(job, "script", PROGRESS_SCRIPT, "A iniciar...")
        try:
            result = await run_generation(job)
        except Exception as exc:  # last-resort: never let a worker die silently
            # Only fill in defaults when the pipeline left the job unmarked: if
            # run_generation already set stage/progress/error we keep those, so the
            # reported stage is the one that actually failed.
            if not job.error:
                # AuthError carries a machine-readable code (AUTH_INVALID_KEY,
                # AUTH_RATE_LIMIT, ...) plus an HTTP status. Surface both so the
                # API layer can map it back onto the shared AUTH_* contract
                # instead of losing the code inside a generic "pipeline:" prefix.
                from backend.services.auth_contract import AuthError
                if isinstance(exc, AuthError):
                    job.error = (
                        f"{exc.error_code}: {exc.message} (HTTP {exc.status_code})"
                    )
                else:
                    job.error = f"pipeline: {exc}"
            if not job.message or job.message == "Na fila.":
                job.message = "Falha inesperada na geração."
            job.status = "failed"
            job.progress = min(job.progress, 0.0)
            _touch(job)
            result = None
        finally:
            _running_ids.discard(job_id)
            await _prune_after_mutation()


async def _prune_after_mutation() -> None:
    """Prune the registry under the lock.

    Non-blocking: if another coroutine is mid-mutation we skip pruning this
    round rather than queue behind it, so a slow mutation can never stall the
    whole worker pool.
    """
    if _lock.locked():
        return
    async with _lock:
        _prune_locked()


def reset_for_tests() -> None:
    """Clear the registry and any module-level state.

    Tests call this in a fixture so job ids and the semaphore never leak between
    cases. The module re-creates the semaphore on import, so resetting it here
    means the next submit_job gets a fresh one.
    """
    global _slot_semaphore
    _jobs.clear()
    _running_ids.clear()
    _slot_semaphore = asyncio.Semaphore(MAX_CONCURRENT_JOBS)


def _stage(job: Job, stage: str, progress: float, message: str) -> None:
    """Publish a progress checkpoint and record it in the job's trail.

    Two properties an external observer depends on:

    * progress never moves backwards - a poller sampling every couple of seconds
      can miss a stage that finishes faster than its interval, but it can never
      watch the value drop;
    * every checkpoint is appended to ``stage_trail``, so a stage that is over
      before the first poll still shows up - with its progress value and its
      timestamp - in the finished job and in the result payload, rather than
      leaving no trace that it ever ran.

    The checkpoint is published before the stage does any work, so the window
    where an observer can catch it is the whole of the stage.
    """
    job.stage = stage
    job.progress = max(float(job.progress), float(progress))
    job.message = message
    _touch(job)
    job.stage_trail.append({
        "stage": stage,
        "progress": job.progress,
        "message": message,
        "at": job.updated_at,
    })


def _fail(job: Job, stage: str, message: str) -> dict:
    """Mark the job FAILED with a pt-PT message and return an empty result."""
    job.status = "failed"
    job.stage = stage
    job.error = message
    job.message = message
    job.progress = 0.0
    _touch(job)
    return {}


# -------------------------------------------------------------------- captions

@dataclass
class _Captions:
    """Caption segments for a narration whose exact text the caller already holds."""

    segments: List[Dict[str, Any]]
    srt_text: str
    duration: float
    duration_source: str
    builder: str
    reasons: List[str] = field(default_factory=list)


def _estimate_narration_duration(text: str) -> float:
    """Rough spoken length of ``text`` in seconds, used only when ffprobe is silent.

    Same rate script_gen uses to size a script, so the estimate lands in the right
    order of magnitude. The caller reports it as an estimate: captions timed off
    it can sit a few tenths of a second away from the real audio.
    """
    words = len(str(text or "").split())
    return round(max(words, 1) / float(script_gen.WORDS_PER_SECOND), 2)


def _normalise_segments(segments: Any) -> List[Dict[str, Any]]:
    """Coerce caption-builder output into the exact shape the SRT writer needs.

    ``build_srt_from_segments`` does ``seg["start"]``/``seg["end"]``/
    ``seg["index"]``/``seg["text"]`` with no tolerance, so anything else a builder
    returns has to be filtered here: a malformed cue must degrade the captions, not
    take the whole render down.
    """
    if not isinstance(segments, list):
        return []
    kept: List[Dict[str, Any]] = []
    for raw in segments:
        if not isinstance(raw, dict):
            continue
        caption = str(raw.get("text") or "").strip()
        if not caption:
            continue
        try:
            start = float(raw.get("start", 0.0))
            end = float(raw.get("end", start))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(start) or not math.isfinite(end) or end < start:
            continue
        kept.append({
            "index": len(kept) + 1,
            "start": round(start, 3),
            "end": round(end, 3),
            "text": caption,
        })
    return kept


def _split_captions_locally(text: str, max_chars: int = DEFAULT_CAPTION_CHARS) -> List[str]:
    """Split ``text`` into caption-sized chunks without the pipeline helper.

    Same contract as ``pipeline._pack_captions``, and deliberately so: whole words
    in input order, packed at sentence boundaries up to ``max_chars``, so every
    caption is a contiguous run of the narration - which is the property that makes
    a caption provably a substring of ``script.full_text``.
    """
    normalised = " ".join(str(text or "").split())
    if not normalised:
        return []
    captions: List[str] = []
    current: List[str] = []
    for sentence in (part.strip() for part in _SENTENCE_SPLIT.split(normalised)):
        words = sentence.split()
        if not words:
            continue
        if current and len(" ".join(current + words)) <= max_chars:
            current.extend(words)
            continue
        if current:
            captions.append(" ".join(current))
            current = []
        for word in words:
            if current and len(" ".join(current + [word])) > max_chars:
                captions.append(" ".join(current))
                current = []
            current.append(word)
    if current:
        captions.append(" ".join(current))
    return captions


def _time_captions(captions: List[str], duration: float) -> List[Dict[str, Any]]:
    """Spread caption chunks across ``duration`` in proportion to their length.

    Reserves the per-caption floor first and shares the remainder by caption
    length, so a cue is never zero-length and the last one always lands on the end
    of the audio: an SRT that claims more time than the narration has makes the
    renderer hold a black frame.
    """
    if not captions:
        return []
    total = float(duration) if math.isfinite(float(duration or 0.0)) else 0.0
    if total <= 0:
        total = MIN_CAPTION_SECONDS * len(captions)
    weights = [float(len(caption)) for caption in captions]
    weight_total = sum(weights) or float(len(captions))
    reserved = min(total, len(captions) * MIN_CAPTION_SECONDS)
    scale = (total - reserved) / weight_total

    segments: List[Dict[str, Any]] = []
    cursor = 0.0
    last = len(captions) - 1
    for position, caption in enumerate(captions):
        start = min(round(cursor, 3), total)
        if position == last:
            end = round(total, 3)
        else:
            end = round(min(total, cursor + max(MIN_CAPTION_SECONDS, weights[position] * scale)), 3)
        if end <= start and segments:
            # The timeline ran out: fold this text into the previous cue rather
            # than drop narration on the floor.
            segments[-1]["text"] = segments[-1]["text"] + " " + caption
            segments[-1]["end"] = round(total, 3)
            continue
        segments.append({
            "index": len(segments) + 1,
            "start": start,
            "end": max(end, round(min(total, start + MIN_CAPTION_SECONDS), 3)),
            "text": caption,
        })
        cursor = end
    return segments


def _build_captions(full_text: str, audio_path: Path) -> _Captions:
    """Caption segments for the narration that was just synthesised.

    WHY NOT TRANSCRIBE: this function is handed ``script.full_text`` - the exact
    string the TTS read. Running ``transcribe_audio_file`` over the audio that
    string produced costs a model load plus a full decode to recover a lossy copy
    of text already in hand, and when faster-whisper is missing it falls through to
    five hardcoded sentences about daily routine and exercise: that is how a video
    narrating Portuguese navigation ended up captioned with commuting and yoga.

    Timings come from the real audio length, measured with the pipeline's own
    ffprobe helper. Only if ffprobe cannot answer does this fall back to a
    word-count estimate, and then it says so instead of pretending the timings are
    measured.

    Runs in a worker thread: it probes the audio and does real text work.
    """
    reasons: List[str] = []

    # Imported here, never at module scope, on purpose. pipeline is the heaviest
    # module in the chain (httpx, dotenv, the provider registry) and this is the
    # one symbol the one-click path wants from it: binding it at import time would
    # make it a hard requirement of importing this module at all, so a pipeline
    # that does not expose it yet would break every caller instead of degrading one
    # job. A lazy import also keeps the lookup honest at call time, which is what
    # lets the fallback below report why it ran.
    build_srt_from_text = None
    try:
        from backend.services.pipeline import build_srt_from_text
    except ImportError:
        reasons.append(
            "As legendas foram divididas localmente: o serviço de pipeline ainda "
            "não disponibiliza build_srt_from_text."
        )
    except Exception as exc:  # a pipeline that fails to load is degraded, not fatal
        reasons.append(
            "As legendas foram divididas localmente: não foi possível carregar "
            f"build_srt_from_text do serviço de pipeline ({exc})."
        )

    probed = pipeline.get_media_duration(audio_path)
    try:
        duration = float(probed) if probed else 0.0
    except (TypeError, ValueError):
        duration = 0.0
    duration_source = "ffprobe"
    if not math.isfinite(duration) or duration <= 0:
        duration_source = "estimate"
        duration = _estimate_narration_duration(full_text)
        reasons.append(
            "O ffprobe não mediu a duração da narração: as legendas estão "
            "espaçadas por uma estimativa a partir do número de palavras."
        )

    builder = "generator-local"
    segments: List[Dict[str, Any]] = []
    if build_srt_from_text is not None:
        try:
            segments = _normalise_segments(build_srt_from_text(full_text, duration))
            builder = "pipeline.build_srt_from_text"
        except Exception as exc:
            reasons.append(
                "O gerador de legendas do pipeline falhou "
                f"({exc}); as legendas foram divididas localmente."
            )
    if not segments:
        builder = "generator-local"
        segments = _normalise_segments(
            _time_captions(_split_captions_locally(full_text), duration)
        )
    if not segments:
        reasons.append("Não foi possível criar legendas: a narração não tem texto.")

    return _Captions(
        segments=segments,
        srt_text=pipeline.build_srt_from_segments(segments),
        duration=round(duration, 3),
        duration_source=duration_source,
        builder=builder,
        reasons=reasons,
    )


# --------------------------------------------------------------------- duration

def _delivered_duration(render_result: Any, storyboard: List[Dict[str, Any]]) -> Optional[float]:
    """Best available measure of the delivered video length, in seconds.

    The renderer's own ffprobe of the finished MP4 first, then the narration it
    muxed in, then the storyboard timeline - in that order of authority, because
    only the first is a measurement of the file the user gets.
    """
    if isinstance(render_result, dict):
        for key in ("video_duration", "audio_duration"):
            value = render_result.get(key)
            try:
                seconds = float(value)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                continue
            if math.isfinite(seconds) and seconds > 0:
                return round(seconds, 2)
    ends = []
    for scene in storyboard or []:
        try:
            ends.append(float(scene.get("end", 0.0)))
        except (TypeError, ValueError):
            continue
    return round(max(ends), 2) if ends and max(ends) > 0 else None


def _duration_report(target: Any, actual: Optional[float]) -> Dict[str, Any]:
    """Compare the delivered duration with the requested one, and say so plainly.

    ``duration_target`` sizes the *script* (script_gen turns it into words per
    section); the narration is synthesised at the voice's natural pace and the
    renderer fits the video to that audio. Nothing here truncates or pads audio
    that has already been synthesised, so a miss cannot be "fixed" by pretending:
    it is reported with the real number and the reason, and the job message
    repeats it, because silently delivering 12 s for a 20 s request is exactly the
    kind of thing a user only discovers after the fact.
    """
    try:
        wanted = int(target)
    except (TypeError, ValueError):
        wanted = 0
    if wanted < 1:
        return {
            "target_seconds": wanted,
            "actual_seconds": actual,
            "honoured": True,
            "note": "",
        }
    if actual is None:
        return {
            "target_seconds": wanted,
            "actual_seconds": None,
            "honoured": False,
            "note": (
                f"O pedido era de {wanted} s e não foi possível medir a duração "
                "final do vídeo para confirmar."
            ),
        }
    drift = abs(actual - wanted)
    tolerance = max(DURATION_TARGET_ABS_TOLERANCE, wanted * DURATION_TARGET_REL_TOLERANCE)
    if drift <= tolerance:
        return {
            "target_seconds": wanted,
            "actual_seconds": actual,
            "honoured": True,
            "note": f"Duração final {actual:.1f} s, dentro da tolerância do pedido ({wanted} s).",
        }
    return {
        "target_seconds": wanted,
        "actual_seconds": actual,
        "honoured": False,
        "note": (
            f"O vídeo ficou com {actual:.1f} s em vez dos {wanted} s pedidos: a "
            "duração entregue segue a narração sintetizada, e duration_target "
            "dimensiona o guião, não o áudio já gravado."
        ),
    }


async def run_generation(job: Job) -> dict:
    """Run the full pipeline for one job, updating state at every checkpoint.

    Stages and their progress values (part of the public contract):
        script   0.05  -> generate_script (fallback is fine, it is recorded)
        voice    0.20  -> synthesize_speech_long + transcribe + build SRT
        media    0.40  -> resolve visual media for the storyboard
        render   0.60  -> render_video_hyperframes
        done     1.00  -> result attached, status = completed

    Any stage failure is caught here: the job becomes failed with a Portuguese
    message and this function returns {} rather than raising. The caller
    (_run_job) has a second catch as a safety net.
    """
    params = job.params
    topic = params.get("topic", "")
    slug = _project_slug(params)
    upload_dir = Path(pipeline.UPLOAD_DIR)
    output_dir = Path(pipeline.OUTPUT_DIR)
    try:
        upload_dir.mkdir(parents=True, exist_ok=True)
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

    # --------------------------------------------------------------- 0.05 script
    _stage(job, "script", PROGRESS_SCRIPT, "A gerar o guião…")
    script = script_gen.generate_script(
        topic,
        language=params.get("language", DEFAULT_LANGUAGE),
        section_count=params.get("section_count", DEFAULT_SECTION_COUNT),
        tone=params.get("tone", DEFAULT_TONE),
        duration_target=params.get("duration_target", DEFAULT_DURATION_TARGET),
        custom_instructions=params.get("custom_instructions", ""),
    )
    script_dict = script.to_dict()
    script_source = getattr(script, "source", "fallback-local")
    fallback_error = getattr(script, "fallback_error", None)
    sections = script_dict.get("sections") or []
    full_text = script_dict.get("full_text") or "\n\n".join(
        (s.get("text") or "") for s in sections
    ).strip()

    # --------------------------------------------------------------- 0.20 voice
    _stage(job, "voice", PROGRESS_VOICE, "A sintetizar a narração…")
    audio_path = upload_dir / f"{slug}.mp3"
    await tts.synthesize_speech_long(
        full_text,
        audio_path,
        voice=params.get("voice", DEFAULT_VOICE),
        provider=params.get("tts_provider", DEFAULT_TTS_PROVIDER),
        rate=params.get("rate", DEFAULT_RATE),
    )
    segments = pipeline.transcribe_audio_file(audio_path) or []
    srt_path = upload_dir / f"{slug}.srt"
    srt_text = pipeline.build_srt_from_segments(segments)
    try:
        srt_path.write_text(srt_text, encoding="utf-8")
    except OSError as exc:
        return _fail(job, "voice", f"Não foi possível escrever a SRT: {exc}")

    # --------------------------------------------------------------- 0.40 media
    _stage(job, "media", PROGRESS_MEDIA, "A buscar imagens de stock…")
    storyboard = pipeline.build_storyboard_from_segments(segments)
    scene_media, term_source = pipeline.search_media_for_scenes(
        storyboard, niche=topic.strip()
    )
    keywords = pipeline.extract_keywords_from_text(srt_text) or [topic.strip()]
    global_media = pipeline.search_media_for_keywords(keywords)
    media_pool = global_media or [
        item for items in scene_media.values() for item in items
    ]
    for idx, scene in enumerate(storyboard):
        candidates = scene_media.get(idx) or []
        media_item = candidates[0] if candidates else {}
        scene["media_url"] = scene.get("media_url") or media_item.get("url", "")
        scene["search_terms"] = [item.get("keyword", "") for item in candidates[:3]]
        scene["caption_style"] = (
            "bottom" if params.get("include_captions", True) else "hidden"
        )
        scene["provider"] = media_item.get("source", "none")

    # ------------------------------------------------------------- 0.60 render
    _stage(job, "render", PROGRESS_RENDER, "A renderizar com HyperFrames…")
    resolved_preset, resolved_style = style.resolve_preset_and_style(
        params.get("preset", DEFAULT_PRESET),
        params.get("subtitle_style"),
    )
    music_track = params.get("music_track")
    if not music_track:
        picked = music.pick_track(
            params.get("music_mood", DEFAULT_MUSIC_MOOD), exclude_ids=None
        )
        music_track = picked.id if picked else None
    render_result = await render_engine.render_video_hyperframes(
        slug,
        srt_path,
        storyboard=storyboard,
        audio_path=audio_path,
        aspect_ratio=params.get("aspect_ratio", "vertical"),
        output_stem=slug,
        preset=params.get("preset", DEFAULT_PRESET),
        subtitle_style=params.get("subtitle_style"),
        music_track=music_track,
        music_volume=params.get("music_volume", DEFAULT_MUSIC_VOLUME),
        duck_voice=params.get("duck_voice", DEFAULT_DUCK_VOICE),
    )
    # The renderer reports status="error" rather than raising when HyperFrames
    # itself fails (no CLI, no memory). Treat that as a stage failure so the job
    # ends FAILED with the renderer's own Portuguese message instead of
    # "completed" with a broken result.
    if isinstance(render_result, dict) and render_result.get("status") == "error":
        return _fail(
            job,
            "render",
            render_result.get("error") or render_result.get("message") or "Falha no render.",
        )

    # ---------------------------------------------------------------- 1.00 done
    _stage(job, "done", PROGRESS_DONE, "Vídeo pronto.")
    payload = {
        "project_name": slug,
        "output_stem": slug,
        "script": script_dict,
        "script_source": script_source,
        "fallback_error": fallback_error,
        "degraded": script_source == "fallback-local",
        "segments": segments,
        "srt_path": str(srt_path),
        "audio_path": str(audio_path),
        "storyboard": storyboard,
        "scene_count": len(storyboard),
        "scenes_with_media": sum(1 for s in storyboard if s.get("media_url")),
        "term_source": term_source,
        "media_pool": media_pool,
        "preset": resolved_preset.name,
        "subtitle_style": resolved_style.to_dict(),
        "music_track": music_track,
        "music_volume": params.get("music_volume", DEFAULT_MUSIC_VOLUME),
        "duck_voice": params.get("duck_voice", DEFAULT_DUCK_VOICE),
        "render": render_result,
    }
    job.status = "completed"
    job.result = payload
    job.message = "Vídeo gerado com sucesso."
    _touch(job)
    return payload
