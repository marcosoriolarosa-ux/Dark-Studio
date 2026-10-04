"""Text-to-Speech: turn a script into narration audio.

Edge TTS is the primary provider because it needs no API key, which is what makes
"topic -> finished video" work for a user who has configured nothing. OpenAI and
Azure adapters exist for users who already have a paying key, and they speak the
same ``AuthError`` contract as the AI gateway so the API layer can map a missing
key to the settings panel without knowing which provider produced it.

Voice catalogue: live first, snapshot second
---------------------------------------------
Microsoft retires Edge voices without warning, and a retired id does not fail as a
bad request - it fails as an opaque ``NoAudioReceived`` that reads exactly like a
network fault. That is how ``pt-PT-RicardoMultilingualNeural`` shipped as
``DEFAULT_VOICE`` and killed a one-click run on the very first attempt.

So there are two sources, and the code never pretends the snapshot is current:

* **Live (truth).** ``edge_tts.list_voices()`` is the same handshake the speech
  call itself uses, and it answers in ~0.1s for the whole catalogue. Measured, so
  the round trip is cheap enough to sit in front of the picker. Cached for
  ``LIVE_CATALOGUE_TTL_SECONDS``.
* **Snapshot (floor).** ``EDGE_VOICES`` is a *verified* offline subset: every id
  in it was read off a live ``list_voices()`` response on
  ``EDGE_VOICES_SNAPSHOT_DATE``, for the locales the product targets. It is what
  the app serves with no network, so a cold or air-gapped install still starts
  and still renders a picker.

Why live-first rather than only-live: only-live makes the picker a network
dependency and turns a Microsoft outage into an empty dropdown. Why keep a
snapshot at all: it keeps ``DEFAULT_VOICE`` and the offline tests meaningful with
no network. The cost is that the snapshot decays - which is why it is stamped
with a date, why ``get_tts_status()`` reports ``voice_catalogue_verified: false``
while serving it, and why a voice the live catalogue does not have raises
``TTS_UNKNOWN_VOICE`` instead of reaching the speech endpoint.

To refresh the snapshot, do not hand-edit it. Read the ids off the live
catalogue::

    python3 -c "import asyncio,json,edge_tts; json.dump(asyncio.run(edge_tts.list_voices()), open('live.json','w'))"

then emit one ``_v(...)`` line per voice you want, in the same locale-then-name
order, and bump ``EDGE_VOICES_SNAPSHOT_DATE``. Hand-editing is how five dead
pt-PT ids - and twenty-five more elsewhere - reached production.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from xml.sax.saxutils import escape as xml_escape

import httpx
from dotenv import load_dotenv

from backend.services.auth_contract import (
    AUTH_INVALID_KEY,
    AUTH_MISSING_KEY,
    AUTH_QUOTA_EXCEEDED,
    AUTH_RATE_LIMIT,
    AUTH_REQUEST_FAILED,
    AuthError,
    missing_key_error,
    provider_status_error,
)
from backend.services.pipeline import BASE_DIR, get_provider_status

# Re-exported on purpose: the API layer imports the whole TTS surface from this
# module, so having the AUTH_* codes next to the adapters that raise them keeps the
# settings-panel routing in one place.
__all__ = [
    "DEFAULT_VOICE",
    "DEFAULT_OPENAI_VOICE",
    "DEFAULT_AZURE_VOICE",
    "EDGE_VOICES",
    "EDGE_VOICES_SNAPSHOT_DATE",
    "SUPPORTED_PROVIDERS",
    "VOICE_ID_PATTERN",
    "VOICES_ENDPOINT",
    "VoiceInfo",
    "confirmed_edge_voice_ids",
    "effective_voice_catalogue",
    "reset_live_catalogue_cache",
    "snapshot_voice_ids",
    "AuthError",
    "TTS_UNKNOWN_VOICE",
    "unknown_voice_error",
    "get_tts_status",
    "is_available",
    "list_voices",
    "split_text_for_speech",
    "synthesize_speech",
    "synthesize_speech_long",
    "AUTH_MISSING_KEY",
    "AUTH_INVALID_KEY",
    "AUTH_RATE_LIMIT",
    "AUTH_QUOTA_EXCEEDED",
    "AUTH_REQUEST_FAILED",
]

# override=True for the same reason as pipeline.py: the settings UI writes .env, and
# a stale ambient OS variable would silently win over the key the user just saved.
load_dotenv(BASE_DIR / ".env", override=True)

try:  # edge-tts is optional so this module still imports without it
    import edge_tts
except Exception:  # pragma: no cover - import guard, exercised by monkeypatching
    edge_tts = None  # type: ignore[assignment]


# Auditioned against the live catalogue on 2026-10-04: the pt-PT voices upstream are
# exactly DuarteNeural and RaquelNeural, so the only two defensible defaults are the
# two below and the retired Ricardo/Duarte/Fernanda/Ines ids are all gone. Chosen
# over DuarteNeural because it is the pt-PT voice the repo's own request body already
# advertises (tests/test_generate_api.py sends voice="pt-PT-RaquelNeural"), so the
# default and the documented example finally agree. See the module docstring for the
# full rationale; both voices synthesise deterministically.
DEFAULT_VOICE = "pt-PT-RaquelNeural"

# The keyed providers get a default voice only when the caller left ours in place:
# the API layer forwards the picked Edge voice, and rejecting it on a keyed provider
# would turn a provider switch into a 400 the user cannot act on.
DEFAULT_OPENAI_VOICE = "alloy"
DEFAULT_AZURE_VOICE = "en-US-JennyNeural"

# A voice id that does not exist is a client mistake, not a credential or quota
# problem. Reporting it as AUTH_REQUEST_FAILED/502 sent operators to the API key
# settings for a bug that lives in this file. Needs a code of its own; auth_contract
# owns the registry and does not define it yet (see the module docstring), so it is
# declared here and re-exported from __all__ until it is added there.
TTS_UNKNOWN_VOICE = "TTS_UNKNOWN_VOICE"
# Named in every unknown-voice message: the picker is the fix, so say where it is.
VOICES_ENDPOINT = "GET /api/voices"

# Date of the live edge_tts.list_voices() response the snapshot below was generated
# from. Stamped, not claimed: after this date an id here may already be retired.
EDGE_VOICES_SNAPSHOT_DATE = "2026-10-04"

# Measured: edge_tts.list_voices() returns the whole catalogue in ~0.1s. The timeout
# is generous enough for a slow link and short enough that a dead network cannot hold
# the voice picker hostage - the snapshot is served instead.
LIVE_CATALOGUE_TTL_SECONDS = 6 * 60 * 60
LIVE_CATALOGUE_TIMEOUT_SECONDS = 5.0
# A failure is remembered briefly too. Without this, an offline install re-arms a
# 5s stall on every single request instead of degrading once.
LIVE_CATALOGUE_RETRY_SECONDS = 60.0

OPENAI_TTS_URL = "https://api.openai.com/v1/audio/speech"
OPENAI_MODEL_ENV = "TTS_OPENAI_MODEL"
OPENAI_DEFAULT_MODEL = "tts-1"
# Azure needs both halves of the pair; the key alone is a regionless credential that
# every request would reject, so we treat them as one setting.
AZURE_KEY_ENV = "AZURE_SPEECH_KEY"
AZURE_REGION_ENV = "AZURE_SPEECH_REGION"
AZURE_OUTPUT_FORMAT = "audio-24khz-48kbitrate-mono-mp3"
AZURE_TTS_HOST = "https://{region}.tts.speech.microsoft.com/cognitiveservices/v1"

# A voice id is interpolated into a provider request, so it is matched against this
# allowlist instead of merely escaped: an id like "../../v1/other" then never reaches
# the network layer, whatever the caller sends.
VOICE_ID_PATTERN = re.compile(r"^[a-z]{2}-[A-Z]{2}-[A-Za-z0-9]+Neural$")

SUPPORTED_PROVIDERS = ("edge", "openai", "azure")

OPENAI_VOICES = {
    "alloy",
    "ash",
    "ballad",
    "coral",
    "echo",
    "fable",
    "onyx",
    "nova",
    "sage",
    "shimmer",
    "verse",
}


@dataclass
class VoiceInfo:
    id: str
    name: str
    gender: str  # "male" | "female" | "unknown"
    locale: str  # e.g. "pt-PT"
    provider: str = "edge"


def _v(voice_id: str, name: str, gender: str, locale: str) -> VoiceInfo:
    return VoiceInfo(id=voice_id, name=name, gender=gender, locale=locale, provider="edge")


def unknown_voice_error(provider: str, voice: str, *, verified_live: bool) -> AuthError:
    """The voice id is not in the catalogue. Names the id and the way to fix it.

    Deliberately its own code and its own status. An unknown voice is a 400 - the
    caller picked something that does not exist - and reusing AUTH_REQUEST_FAILED
    (502) is what made a retired default voice look like a broken API key for a
    month.

    ``verified_live`` says whether the live catalogue actually answered. It changes
    the wording, not the code: when the catalogue could not be reached, "not in the
    catalogue" may just mean "we could not check", and the message says so instead of
    asserting something it does not know.
    """
    if verified_live:
        message = (
            f"Unknown {provider} voice id {voice!r}: it is not in the current Edge "
            f"voice catalogue. Pick a live id from {VOICES_ENDPOINT}, e.g. "
            f"{DEFAULT_VOICE!r}."
        )
    else:
        message = (
            f"Unknown {provider} voice id {voice!r}: the live Edge voice catalogue "
            f"could not be reached to confirm it, and it is not in the offline "
            f"snapshot either. Pick a live id from {VOICES_ENDPOINT}, e.g. "
            f"{DEFAULT_VOICE!r}."
        )
    return AuthError(
        TTS_UNKNOWN_VOICE,
        message,
        400,
        {
            "provider": provider,
            "voice": voice,
            "voices_endpoint": VOICES_ENDPOINT,
            "default_voice": DEFAULT_VOICE,
            "catalogue_verified_live": verified_live,
        },
    )


# --------------------------------------------------------------------------- #
# Offline snapshot. Generated from a live edge_tts.list_voices() response on
# EDGE_VOICES_SNAPSHOT_DATE, never hand-edited. Locales are the ones the product
# offers (script_gen.LANGUAGES plus the en-GB and de-DE locales the picker has
# always carried). Ordered by locale, then by voice, so the picker is stable
# between calls and between processes; the API layer does not sort and a reshuffled
# list reads like a bug.
# --------------------------------------------------------------------------- #
EDGE_VOICES: List[VoiceInfo] = [
    # de-DE.
    _v("de-DE-AmalaNeural", "Amala", "female", "de-DE"),
    _v("de-DE-ConradNeural", "Conrad", "male", "de-DE"),
    _v("de-DE-FlorianMultilingualNeural", "FlorianMultilingual", "male", "de-DE"),
    _v("de-DE-KatjaNeural", "Katja", "female", "de-DE"),
    _v("de-DE-KillianNeural", "Killian", "male", "de-DE"),
    _v("de-DE-SeraphinaMultilingualNeural", "SeraphinaMultilingual", "female", "de-DE"),
    # en-GB.
    _v("en-GB-LibbyNeural", "Libby", "female", "en-GB"),
    _v("en-GB-MaisieNeural", "Maisie", "female", "en-GB"),
    _v("en-GB-RyanNeural", "Ryan", "male", "en-GB"),
    _v("en-GB-SoniaNeural", "Sonia", "female", "en-GB"),
    _v("en-GB-ThomasNeural", "Thomas", "male", "en-GB"),
    # en-US.
    _v("en-US-AndrewMultilingualNeural", "Andrew", "male", "en-US"),
    _v("en-US-AndrewNeural", "Andrew", "male", "en-US"),
    _v("en-US-AnaNeural", "Ana", "female", "en-US"),
    _v("en-US-AriaNeural", "Aria", "female", "en-US"),
    _v("en-US-AvaMultilingualNeural", "Ava", "female", "en-US"),
    _v("en-US-AvaNeural", "Ava", "female", "en-US"),
    _v("en-US-BrianMultilingualNeural", "Brian", "male", "en-US"),
    _v("en-US-BrianNeural", "Brian", "male", "en-US"),
    _v("en-US-ChristopherNeural", "Christopher", "male", "en-US"),
    _v("en-US-EmmaMultilingualNeural", "Emma", "female", "en-US"),
    _v("en-US-EmmaNeural", "Emma", "female", "en-US"),
    _v("en-US-EricNeural", "Eric", "male", "en-US"),
    _v("en-US-GuyNeural", "Guy", "male", "en-US"),
    _v("en-US-JennyNeural", "Jenny", "female", "en-US"),
    _v("en-US-MichelleNeural", "Michelle", "female", "en-US"),
    _v("en-US-RogerNeural", "Roger", "male", "en-US"),
    _v("en-US-SteffanNeural", "Steffan", "male", "en-US"),
    # es-ES.
    _v("es-ES-AlvaroNeural", "Alvaro", "male", "es-ES"),
    _v("es-ES-ElviraNeural", "Elvira", "female", "es-ES"),
    _v("es-ES-XimenaNeural", "Ximena", "female", "es-ES"),
    # fr-FR.
    _v("fr-FR-DeniseNeural", "Denise", "female", "fr-FR"),
    _v("fr-FR-EloiseNeural", "Eloise", "female", "fr-FR"),
    _v("fr-FR-HenriNeural", "Henri", "male", "fr-FR"),
    _v("fr-FR-RemyMultilingualNeural", "Remy", "male", "fr-FR"),
    _v("fr-FR-VivienneMultilingualNeural", "Vivienne", "female", "fr-FR"),
    # pt-BR.
    _v("pt-BR-AntonioNeural", "Antonio", "male", "pt-BR"),
    _v("pt-BR-FranciscaNeural", "Francisca", "female", "pt-BR"),
    _v("pt-BR-ThalitaMultilingualNeural", "ThalitaMultilingual", "female", "pt-BR"),
    # pt-PT.
    _v("pt-PT-DuarteNeural", "Duarte", "male", "pt-PT"),
    _v("pt-PT-RaquelNeural", "Raquel", "female", "pt-PT"),
]


# --------------------------------------------------------------------------- #
# Live catalogue.
# --------------------------------------------------------------------------- #
_live_lock = threading.Lock()
_live_voices: Optional[List[VoiceInfo]] = None
_live_checked_at: float = 0.0
_live_attempted_at: float = 0.0


def _live_display_name(record: Dict[str, Any], voice_id: str) -> str:
    """Turn ``Microsoft Duarte Online (Natural) - Portuguese (Portugal)`` into ``Duarte``.

    Falls back to the id with its locale prefix stripped, so a record without a
    friendly name still renders something in the picker instead of a blank row.
    """
    friendly = str(record.get("FriendlyName") or "")
    if friendly.startswith("Microsoft "):
        friendly = friendly[len("Microsoft "):]
    name = friendly.split(" Online")[0].strip()
    if name:
        return name
    locale = str(record.get("Locale") or "")
    return voice_id[len(locale) + 1:] if locale and voice_id.startswith(locale + "-") else voice_id


def _voice_from_live(record: Dict[str, Any]) -> Optional[VoiceInfo]:
    """One live catalogue entry, or None when this app must not offer it.

    Two filters, both load-bearing:

    * The id must match ``VOICE_ID_PATTERN``. Eight live ids do not (the three-letter
      ``fil-PH`` pair, ``zh-CN-liaoning``/``zh-CN-shaanxi`` and the ``*-Latn-CA`` /
      ``*-Cans-CA`` pairs). They are filtered by the *request* layer, so offering
      them in the picker would hand the user an id the app itself refuses - the
      catalogue must never be wider than what ``_validate_voice`` accepts.
    * ``Status == "Deprecated"`` is dropped. Nothing is deprecated today; if Microsoft
      starts marking retiring voices, they stop being offered instead of being offered
      and then failing with ``NoAudioReceived``.
    """
    try:
        voice_id = str(record.get("ShortName") or "")
        locale = str(record.get("Locale") or "")
        if not voice_id or not locale or not VOICE_ID_PATTERN.match(voice_id):
            return None
        if str(record.get("Status") or "GA") == "Deprecated":
            return None
        gender = {"male": "male", "female": "female"}.get(
            str(record.get("Gender") or "").strip().lower(), "unknown"
        )
        return VoiceInfo(
            id=voice_id,
            name=_live_display_name(record, voice_id),
            gender=gender,
            locale=locale,
            provider="edge",
        )
    except Exception:
        return None


def _run_list_voices() -> List[VoiceInfo]:
    """Call ``edge_tts.list_voices()`` and normalise it.

    Run on a private thread (see ``_live_voice_catalogue``) so this coroutine never
    has to share a loop with the caller.
    """
    lister = getattr(edge_tts, "list_voices", None)
    if not callable(lister):
        raise RuntimeError("edge-tts exposes no list_voices()")

    async def _collect() -> List[VoiceInfo]:
        records = await lister()
        voices = [_voice_from_live(dict(record)) for record in records or []]
        return [voice for voice in voices if voice is not None]

    return asyncio.run(_collect())


def _live_voice_catalogue(refresh: bool = False) -> Optional[List[VoiceInfo]]:
    """The live catalogue, or None when it cannot be had. Never raises.

    Served from a TTL cache. A successful fetch is cached for
    ``LIVE_CATALOGUE_TTL_SECONDS``; a *failure* is remembered for
    ``LIVE_CATALOGUE_RETRY_SECONDS`` so an offline install degrades once instead of
    stalling every request for the timeout, and so the outage is visible in
    ``get_tts_status()`` rather than papered over.

    The coroutine runs on a daemon thread joined with a hard timeout: callers are both
    sync (the FastAPI threadpool behind ``GET /api/voices``) and async (the pipeline),
    and a fresh thread is the only shape that is safe in both without an
    already-running-loop check at every call site. A fetch that outlives the timeout is
    abandoned, not killed - ``daemon=True`` keeps it from blocking interpreter exit.
    """
    global _live_voices, _live_checked_at, _live_attempted_at

    with _live_lock:
        now = time.monotonic()
        if not refresh:
            if _live_voices is not None and now - _live_checked_at < LIVE_CATALOGUE_TTL_SECONDS:
                return _live_voices
            if _live_voices is None and now - _live_attempted_at < LIVE_CATALOGUE_RETRY_SECONDS:
                return None
        _live_attempted_at = now

        if edge_tts is None:
            return None

        outcome: Dict[str, Any] = {}

        def _runner() -> None:
            try:
                outcome["voices"] = _run_list_voices()
            except BaseException as exc:  # noqa: BLE001 - any failure means "no live list"
                outcome["error"] = exc

        thread = threading.Thread(target=_runner, name="edge-voice-catalogue", daemon=True)
        thread.start()
        thread.join(LIVE_CATALOGUE_TIMEOUT_SECONDS)

        if thread.is_alive() or "voices" not in outcome or not outcome["voices"]:
            # Keep the previous good list if we had one; an expired-but-usable
            # catalogue beats a hard drop to 41 snapshot entries.
            if _live_voices is not None:
                _live_checked_at = now
                return _live_voices
            return None

        _live_voices = outcome["voices"]
        _live_checked_at = time.monotonic()
        return _live_voices


def reset_live_catalogue_cache() -> None:
    """Drop the cached live list. For tests and for a deliberate refresh."""
    global _live_voices, _live_checked_at, _live_attempted_at
    with _live_lock:
        _live_voices = None
        _live_checked_at = 0.0
        _live_attempted_at = 0.0


def confirmed_edge_voice_ids() -> Optional[Set[str]]:
    """Ids proven to exist upstream right now, or None when nothing was confirmed.

    The distinction matters to the error contract: "not in the catalogue" is a fact
    when this returns a set and only a suspicion when it returns None.
    """
    live = _live_voice_catalogue()
    if not live:
        return None
    return {voice.id for voice in live}


def effective_voice_catalogue() -> List[VoiceInfo]:
    """Live catalogue when reachable, the verified snapshot otherwise.

    Sorted here rather than at the fetch site, so "stable between calls and between
    processes" is a property of this function and holds for every source. Upstream
    order is not stable - Microsoft re-sorts as voices are added - and the API layer
    does not sort, so a reshuffled picker reads like a bug to the user.
    """
    live = _live_voice_catalogue()
    catalogue = live if live else EDGE_VOICES
    return sorted(catalogue, key=lambda voice: (voice.locale, voice.id))


def list_voices(locale: str = "") -> List[VoiceInfo]:
    """Edge voices, optionally narrowed to a locale prefix.

    Live catalogue first, verified snapshot when the network is not there - so the
    picker shows every voice that really exists, and still renders offline.

    ``locale`` matches by prefix, so ``"pt"`` returns pt-PT and pt-BR while
    ``"pt-PT"`` returns only Portugal. Never raises: the voice picker is the first
    thing the UI asks for and an exception there would take down the whole form.
    """
    try:
        wanted = (locale or "").strip().lower()
        catalogue = effective_voice_catalogue()
        if not wanted:
            return list(catalogue)
        return [voice for voice in catalogue if voice.locale.lower().startswith(wanted)]
    except Exception:
        return list(EDGE_VOICES)


def _edge_installed() -> bool:
    return edge_tts is not None


def is_available(provider: str = "edge") -> bool:
    """Whether `provider` can run right now. Reports, never raises.

    The settings page calls this to build the provider list, so an unknown name or
    a broken environment reports False instead of failing the request.
    """
    try:
        name = (provider or "").strip().lower()
        if name == "edge":
            return _edge_installed()
        if name == "openai":
            return bool(os.getenv("OPENAI_API_KEY", "").strip())
        if name == "azure":
            return bool(
                os.getenv(AZURE_KEY_ENV, "").strip()
                and os.getenv(AZURE_REGION_ENV, "").strip()
            )
        return False
    except Exception:
        return False


def get_tts_status() -> Dict[str, Any]:
    """Status blob for the UI, reusing the AI gateway's provider status shape.

    ``voices`` is the size of the catalogue actually being served, and
    ``voice_catalogue_verified`` says whether that catalogue is live. Reporting a
    confident count off a months-old snapshot is what let the UI offer five dead
    pt-PT voices; when the live list cannot be reached, the blob now says so and
    carries a note instead of implying full confidence.
    """
    live = _live_voice_catalogue()
    catalogue = live if live else EDGE_VOICES
    status: Dict[str, Any] = {
        "providers": {
            name: {"available": is_available(name), "requires_key": name != "edge"}
            for name in SUPPORTED_PROVIDERS
        },
        "default_voice": DEFAULT_VOICE,
        "default_provider": "edge",
        "voices": len(catalogue),
        "voice_source": "live" if live else "offline_snapshot",
        "voice_catalogue_verified": live is not None,
        "ai_gateway": get_provider_status(),
    }
    if live is None:
        status["voice_catalogue_snapshot_date"] = EDGE_VOICES_SNAPSHOT_DATE
        status["voice_catalogue_note"] = (
            f"Live Edge voice catalogue unreachable; these {len(EDGE_VOICES)} voices are "
            f"the offline snapshot taken on {EDGE_VOICES_SNAPSHOT_DATE}, not a live "
            "listing. A voice retired upstream since then will be rejected rather than "
            f"listed. Available ids: {VOICES_ENDPOINT}."
        )
    return status


def _validate_voice(voice: str, provider: str) -> str:
    """Reject an unusable voice id before any network call happens.

    This runs ahead of the request so a typo in the settings form costs nothing and
    a hostile id never reaches a URL.

    Two different mistakes, two different errors. A malformed id is a plain
    ``ValueError`` - a client bug that no provider could ever satisfy. A *well-formed*
    id that is not in the catalogue is ``TTS_UNKNOWN_VOICE``: naming it, pointing at
    ``GET /api/voices``, and never pretending the credential or the quota is at
    fault. The membership check reads the live catalogue when it is reachable, which
    is the whole point - the shipped bug passed validation precisely because the
    retired id was sitting in the static list.
    """
    candidate = (voice or "").strip()
    if not candidate:
        raise ValueError("Voice id is required.")

    if provider == "edge":
        if not VOICE_ID_PATTERN.match(candidate):
            raise ValueError(f"Invalid Edge voice id: {candidate!r}")
        confirmed = confirmed_edge_voice_ids()
        known = confirmed if confirmed is not None else {voice.id for voice in EDGE_VOICES}
        if candidate not in known:
            raise unknown_voice_error(
                "edge", candidate, verified_live=confirmed is not None
            )
        return candidate

    if provider == "openai":
        if candidate not in OPENAI_VOICES:
            raise ValueError(f"Unknown OpenAI voice: {candidate!r}")
        return candidate

    if provider == "azure":
        if not VOICE_ID_PATTERN.match(candidate):
            raise ValueError(f"Invalid Azure voice id: {candidate!r}")
        return candidate

    raise ValueError(f"Unknown TTS provider: {provider!r}")


def _resolve_voice(voice: str, provider: str) -> str:
    """Swap in the provider default when the caller left ours in place."""
    candidate = (voice or "").strip()
    if candidate != DEFAULT_VOICE:
        return candidate
    if provider == "openai":
        return DEFAULT_OPENAI_VOICE
    if provider == "azure":
        return DEFAULT_AZURE_VOICE
    return candidate


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def _split_long_sentence(sentence: str, max_chars: int) -> List[str]:
    """Break an over-long sentence at word boundaries.

    A word longer than the budget (a URL in a script, typically) has no legal
    boundary, so it is hard-cut: that is the only case where a word is split, and
    refusing to split it would loop forever.
    """
    pieces: List[str] = []
    remaining = sentence
    while len(remaining) > max_chars:
        window = remaining[:max_chars]
        cut = window.rfind(" ")
        if cut <= 0:
            pieces.append(window)
            remaining = remaining[max_chars:]
        else:
            pieces.append(window[:cut])
            remaining = remaining[cut + 1:]
    if remaining:
        pieces.append(remaining)
    return [piece for piece in pieces if piece]


def split_text_for_speech(text: str, max_chars: int = 1800) -> List[str]:
    """Split a script into chunks Edge TTS will accept in one request.

    Splitting happens on sentence boundaries so the narration keeps its rhythm, and
    inside an over-long sentence on word boundaries so a chunk never ends mid-word.
    Edge rejects very long single requests outright, and a word cut in half is
    pronounced as garbage, which is why this is not a plain ``textwrap`` call.
    """
    if max_chars <= 0:
        raise ValueError("max_chars must be positive.")

    normalized = _normalize_text(text)
    if not normalized:
        return []

    sentences = re.split(r"(?<=[.!?...])\s+", normalized)
    chunks: List[str] = []
    current = ""
    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue
        if len(sentence) > max_chars:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(_split_long_sentence(sentence, max_chars))
            continue
        if not current:
            current = sentence
        elif len(current) + 1 + len(sentence) <= max_chars:
            current = f"{current} {sentence}"
        else:
            chunks.append(current)
            current = sentence
    if current:
        chunks.append(current)
    return chunks


def _strip_id3(payload: bytes) -> bytes:
    """Drop a leading ID3v2 tag and a trailing ID3v1 block.

    Chunks are joined by raw byte concatenation: no ffmpeg dependency, and Edge
    returns one MPEG stream per chunk. An ID3 tag sitting mid-stream is what makes
    naive concatenation audibly glitch, so only the first chunk keeps its tag.
    """
    if payload[:3] == b"ID3" and len(payload) >= 10:
        size = 0
        for byte in payload[6:10]:
            size = (size << 7) | (byte & 0x7F)
        # A tag claiming to be larger than the file is a malformed stream, not a
        # tag; stripping on it would silently truncate the narration to nothing.
        if 0 <= size <= len(payload) - 10:
            payload = payload[10 + size:]
    if payload[-128:-125] == b"TAG":
        payload = payload[:-128]
    return payload


def _write_audio(output_path: Path, payload: bytes) -> Path:
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    return target


def _is_unknown_voice_signal(exc: Exception) -> bool:
    """Whether `exc` is Edge's way of saying "that voice id does not exist".

    ``edge_tts`` raises ``NoAudioReceived("No audio was received. Please verify that
    your parameters are correct.")`` for a retired id - the same thing it raises for
    nonsense prosody. Matched on the class name and the message rather than imported,
    because the exception moved between edge-tts releases and the app supports more
    than one.
    """
    if type(exc).__name__ == "NoAudioReceived":
        return True
    text = str(exc).lower()
    return "no audio was received" in text or "no audio received" in text


def snapshot_voice_ids() -> Set[str]:
    """Every id in the offline snapshot."""
    return {voice.id for voice in EDGE_VOICES}


def _edge_no_audio_error(voice: str, confirmed: Optional[Set[str]], reason: str) -> AuthError:
    """Edge answered with no audio. Decide - honestly - what that means.

    ``confirmed is None`` means the live catalogue could not be reached, so an empty
    stream is ambiguous: a retired id and a rejected prosody look identical upstream.
    When the id is *also* absent from the offline snapshot we have positive reason to
    suspect retirement, and report it with its own code.

    Otherwise it stays ``AUTH_REQUEST_FAILED``. Asserting "unknown voice" for an id we
    never checked would be the same over-confident mislabelling as the bug that got
    fixed, just pointed the other way - an operator would be told to pick a different
    voice when the real fix is the prosody or the provider. So the status and code
    stay put, and the message names the id and points at the picker.
    """
    if confirmed is None and voice not in snapshot_voice_ids():
        return unknown_voice_error("edge", voice, verified_live=False)

    details: Dict[str, Any] = {
        "provider": "edge",
        "voice": voice,
        "voices_endpoint": VOICES_ENDPOINT,
    }
    message = f"Edge TTS produced no audio for voice {voice!r}: {reason}"
    if confirmed is None:
        details["catalogue_verified_live"] = False
        message += (
            " The live voice catalogue could not be reached to confirm the id; if this "
            f"persists, check it against {VOICES_ENDPOINT}."
        )
    return AuthError(AUTH_REQUEST_FAILED, message, 502, details)


async def _synthesize_edge(
    text: str,
    output_path: Path,
    voice: str,
    rate: str,
    volume: str,
    pitch: str,
) -> Path:
    if not _edge_installed():
        # Named explicitly: the actionable fix is "pip install edge-tts", which a
        # generic RuntimeError message does not hand the user.
        raise RuntimeError(
            "Text-to-speech is unavailable: the optional package 'edge-tts' is not "
            "installed. Run 'pip install edge-tts' to enable free narration."
        )

    # Resolved once, here, and reused for the no-audio verdict below: a fresh live
    # lookup per branch would double the network cost of the failure path.
    confirmed = confirmed_edge_voice_ids()

    try:
        communicate = edge_tts.Communicate(text, voice, rate=rate, volume=volume, pitch=pitch)
        await communicate.save(str(output_path))
    except AuthError:
        raise
    except Exception as exc:  # edge_tts raises its own connection/audio errors
        # This is the path the shipped bug took: a retired voice id reached Edge, Edge
        # answered with an empty stream, and the wrapper reported a 502 that points at
        # credentials. Now the id is rejected pre-flight against the live catalogue, so
        # the only way back here is a catalogue that changed under us mid-run.
        if _is_unknown_voice_signal(exc):
            raise _edge_no_audio_error(voice, confirmed, str(exc)) from exc
        raise AuthError(
            AUTH_REQUEST_FAILED,
            f"Edge TTS failed to synthesize the narration: {exc}",
            502,
            {
                "provider": "edge",
                "exception": type(exc).__name__,
                "voice": voice,
                "voices_endpoint": VOICES_ENDPOINT,
            },
        ) from exc

    if not output_path.exists() or output_path.stat().st_size == 0:
        raise _edge_no_audio_error(voice, confirmed, "the response stream was empty")
    return output_path


async def _synthesize_openai(text: str, output_path: Path, voice: str, model: str) -> Path:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise missing_key_error("openai")

    payload: Dict[str, Any] = {
        "model": model,
        "voice": voice,
        "input": text,
        "response_format": "mp3",
    }
    # rate/volume/pitch have no OpenAI equivalent. They are accepted for provider
    # parity and ignored here rather than faked as a speed multiplier, which would
    # change the delivery instead of the pacing.

    try:
        # Sync httpx.post keeps this mockable the same way provider_registry does.
        response = httpx.post(
            OPENAI_TTS_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=120.0,
        )
    except httpx.HTTPError as exc:
        raise AuthError(
            AUTH_REQUEST_FAILED,
            f"Network error contacting OpenAI speech: {exc}",
            502,
            {"provider": "openai", "exception": type(exc).__name__},
        ) from exc

    if response.status_code != 200:
        raise provider_status_error("openai", response.status_code, response.text, model)

    return _write_audio(output_path, response.content)


def _azure_ssml(text: str, voice: str, rate: str, volume: str, pitch: str) -> str:
    locale = "-".join(voice.split("-")[:2]) or "en-US"
    return (
        "<speak version='1.0' xmlns='http://www.w3.org/2001/10/synthesis' xml:lang='%s'>"
        "<voice name='%s'>"
        "<prosody rate='%s' volume='%s' pitch='%s'>%s</prosody>"
        "</voice></speak>"
    ) % (locale, voice, rate, volume, pitch, xml_escape(text))


async def _synthesize_azure(
    text: str,
    output_path: Path,
    voice: str,
    rate: str,
    volume: str,
    pitch: str,
) -> Path:
    key = os.getenv(AZURE_KEY_ENV, "").strip()
    region = os.getenv(AZURE_REGION_ENV, "").strip()
    if not key or not region:
        # Reported as a missing key rather than a generic failure so the settings
        # panel opens on the Azure field instead of showing an opaque 502.
        raise missing_key_error("azure")

    try:
        response = httpx.post(
            AZURE_TTS_HOST.format(region=region),
            headers={
                "Ocp-Apim-Subscription-Key": key,
                "Content-Type": "application/ssml+xml",
                "X-Microsoft-OutputFormat": AZURE_OUTPUT_FORMAT,
                "User-Agent": "Dark-Studio",
            },
            content=_azure_ssml(text, voice, rate, volume, pitch).encode("utf-8"),
            timeout=120.0,
        )
    except httpx.HTTPError as exc:
        raise AuthError(
            AUTH_REQUEST_FAILED,
            f"Network error contacting Azure speech: {exc}",
            502,
            {"provider": "azure", "exception": type(exc).__name__},
        ) from exc

    if response.status_code != 200:
        raise provider_status_error("azure", response.status_code, response.text)

    return _write_audio(output_path, response.content)


async def synthesize_speech(
    text: str,
    output_path: Path,
    voice: str = DEFAULT_VOICE,
    *,
    provider: str = "edge",
    rate: str = "+0%",
    volume: str = "+0%",
    pitch: str = "+0Hz",
) -> Path:
    """Render `text` to an MP3 at `output_path` and return that path.

    Raises ValueError for empty text or a malformed voice id (always before any
    network call), AuthError with ``TTS_UNKNOWN_VOICE`` for a well-formed id that is
    not in the voice catalogue, AuthError with the AUTH_* codes for a missing or
    rejected key or a genuine provider failure, and RuntimeError when the optional
    edge-tts package is not installed.
    """
    normalized = _normalize_text(text)
    if not normalized:
        raise ValueError("Text to synthesize cannot be empty.")

    provider_name = (provider or "edge").strip().lower()
    if provider_name not in SUPPORTED_PROVIDERS:
        raise ValueError(f"Unknown TTS provider: {provider!r}")

    resolved_voice = _validate_voice(_resolve_voice(voice, provider_name), provider_name)
    target = Path(output_path)

    if provider_name == "edge":
        return await _synthesize_edge(normalized, target, resolved_voice, rate, volume, pitch)
    if provider_name == "openai":
        model = os.getenv(OPENAI_MODEL_ENV, "").strip() or OPENAI_DEFAULT_MODEL
        return await _synthesize_openai(normalized, target, resolved_voice, model)
    return await _synthesize_azure(normalized, target, resolved_voice, rate, volume, pitch)


async def synthesize_speech_long(
    text: str, output_path: Path, max_chars: int = 1800, **kwargs: Any
) -> Path:
    """``synthesize_speech`` for scripts too long for a single request.

    Renders each chunk and joins the audio. Chunks go to a temp directory next to the
    target so the join reads from the same filesystem, and the directory is
    removed even when a chunk fails.
    """
    chunks = split_text_for_speech(text, max_chars)
    if not chunks:
        raise ValueError("Text to synthesize cannot be empty.")

    target = Path(output_path)
    if len(chunks) == 1:
        return await synthesize_speech(chunks[0], target, **kwargs)

    target.parent.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(tempfile.mkdtemp(prefix="tts_chunks_", dir=str(target.parent)))
    try:
        parts: List[bytes] = []
        for index, chunk in enumerate(chunks):
            chunk_path = temp_dir / f"part_{index:03d}.mp3"
            await synthesize_speech(chunk, chunk_path, **kwargs)
            payload = chunk_path.read_bytes()
            parts.append(payload if index == 0 else _strip_id3(payload))
        return _write_audio(target, b"".join(parts))
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
