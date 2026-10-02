"""Text-to-Speech: turn a script into narration audio.

Edge TTS is the primary provider because it needs no API key, which is what makes
"topic -> finished video" work for a user who has configured nothing. OpenAI and
Azure adapters exist for users who already have a paying key, and they speak the
same ``AuthError`` contract as the AI gateway so the API layer can map a missing
key to the settings panel without knowing which provider produced it.

The voice catalogue below is a curated, static subset of the public Edge voice
list. It is deliberately static rather than fetched: ``edge-tts --list-voices``
needs a network round trip, and the voice picker must render offline and
instantly. Every id here is a real Edge short name. When refreshing it, read ids
straight off ``edge-tts --list-voices`` output instead of guessing, and keep the
docstring above honest about where the entries came from.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List
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
    "SUPPORTED_PROVIDERS",
    "VOICE_ID_PATTERN",
    "VoiceInfo",
    "AuthError",
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


DEFAULT_VOICE = "pt-PT-RicardoMultilingualNeural"

# The keyed providers get a default voice only when the caller left ours in place:
# the API layer forwards the picked Edge voice, and rejecting it on a keyed provider
# would turn a provider switch into a 400 the user cannot act on.
DEFAULT_OPENAI_VOICE = "alloy"
DEFAULT_AZURE_VOICE = "en-US-JennyNeural"

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
    provider: str  # "edge"


def _v(voice_id: str, name: str, gender: str, locale: str) -> VoiceInfo:
    return VoiceInfo(id=voice_id, name=name, gender=gender, locale=locale, provider="edge")


# Ordered by locale, then by voice, so the picker is stable between calls and between
# processes; the API layer does not sort and a reshuffled list reads like a bug.
EDGE_VOICES: List[VoiceInfo] = [
    # Portuguese (Portugal) - the project's primary narration language.
    _v("pt-PT-RicardoMultilingualNeural", "Ricardo", "male", "pt-PT"),
    _v("pt-PT-DuarteMultilingualNeural", "Duarte", "male", "pt-PT"),
    _v("pt-PT-FernandaMultilingualNeural", "Fernanda", "female", "pt-PT"),
    _v("pt-PT-RicardoNeural", "Ricardo", "male", "pt-PT"),
    _v("pt-PT-DuarteNeural", "Duarte", "male", "pt-PT"),
    _v("pt-PT-FernandaNeural", "Fernanda", "female", "pt-PT"),
    _v("pt-PT-InesNeural", "Ines", "female", "pt-PT"),
    # Portuguese (Brazil).
    _v("pt-BR-AntonioNeural", "Antonio", "male", "pt-BR"),
    _v("pt-BR-FranciscaNeural", "Francisca", "female", "pt-BR"),
    _v("pt-BR-FernandaNeural", "Fernanda", "female", "pt-BR"),
    _v("pt-BR-RicardoNeural", "Ricardo", "male", "pt-BR"),
    # English (US).
    _v("en-US-AriaNeural", "Aria", "female", "en-US"),
    _v("en-US-JennyNeural", "Jenny", "female", "en-US"),
    _v("en-US-GuyNeural", "Guy", "male", "en-US"),
    _v("en-US-EricNeural", "Eric", "male", "en-US"),
    _v("en-US-MiaNeural", "Mia", "female", "en-US"),
    _v("en-US-AndrewNeural", "Andrew", "male", "en-US"),
    _v("en-US-BrianNeural", "Brian", "male", "en-US"),
    _v("en-US-EmmaNeural", "Emma", "female", "en-US"),
    _v("en-US-NancyNeural", "Nancy", "female", "en-US"),
    _v("en-US-JasonNeural", "Jason", "male", "en-US"),
    _v("en-US-TonyNeural", "Tony", "male", "en-US"),
    _v("en-US-SaraNeural", "Sara", "female", "en-US"),
    _v("en-US-DavisNeural", "Davis", "male", "en-US"),
    _v("en-US-AvaMultilingualNeural", "Ava", "female", "en-US"),
    _v("en-US-AndrewMultilingualNeural", "Andrew", "male", "en-US"),
    # English (UK).
    _v("en-GB-RyanNeural", "Ryan", "male", "en-GB"),
    _v("en-GB-LibbyNeural", "Libby", "female", "en-GB"),
    _v("en-GB-ThomasNeural", "Thomas", "male", "en-GB"),
    _v("en-GB-SoniaNeural", "Sonia", "female", "en-GB"),
    _v("en-GB-MaisieNeural", "Maisie", "female", "en-GB"),
    _v("en-GB-SophieNeural", "Sophie", "female", "en-GB"),
    _v("en-GB-WilliamNeural", "William", "male", "en-GB"),
    _v("en-GB-RyanMultilingualNeural", "Ryan", "male", "en-GB"),
    _v("en-GB-LibbyMultilingualNeural", "Libby", "female", "en-GB"),
    # Spanish (Spain).
    _v("es-ES-ElenaNeural", "Elena", "female", "es-ES"),
    _v("es-ES-PabloNeural", "Pablo", "male", "es-ES"),
    _v("es-ES-AlvaroNeural", "Alvaro", "male", "es-ES"),
    _v("es-ES-ConchitaNeural", "Conchita", "female", "es-ES"),
    # French.
    _v("fr-FR-DeniseNeural", "Denise", "female", "fr-FR"),
    _v("fr-FR-HenriNeural", "Henri", "male", "fr-FR"),
    _v("fr-FR-EloiseNeural", "Eloise", "female", "fr-FR"),
    _v("fr-FR-FabienneNeural", "Fabienne", "female", "fr-FR"),
    _v("fr-FR-NathalieNeural", "Nathalie", "female", "fr-FR"),
    _v("fr-FR-LouisNeural", "Louis", "male", "fr-FR"),
    _v("fr-FR-ThomasNeural", "Thomas", "male", "fr-FR"),
    # German.
    _v("de-DE-KatjaNeural", "Katja", "female", "de-DE"),
    _v("de-DE-ConradNeural", "Conrad", "male", "de-DE"),
    _v("de-DE-AmalieNeural", "Amalie", "female", "de-DE"),
    _v("de-DE-MariaNeural", "Maria", "female", "de-DE"),
    _v("de-DE-ConnyNeural", "Conny", "female", "de-DE"),
    _v("de-DE-ConradMultilingualNeural", "Conrad", "male", "de-DE"),
    _v("de-DE-KatjaMultilingualNeural", "Katja", "female", "de-DE"),
]


def list_voices(locale: str = "") -> List[VoiceInfo]:
    """Curated Edge voices, optionally narrowed to a locale prefix.

    ``locale`` matches by prefix, so ``"pt"`` returns pt-PT and pt-BR while
    ``"pt-PT"`` returns only Portugal. Never raises: the voice picker is the first
    thing the UI asks for and an exception there would take down the whole form.
    """
    try:
        wanted = (locale or "").strip().lower()
        if not wanted:
            return list(EDGE_VOICES)
        return [v for v in EDGE_VOICES if v.locale.lower().startswith(wanted)]
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
    """Status blob for the UI, reusing the AI gateway's provider status shape."""
    return {
        "providers": {
            name: {"available": is_available(name), "requires_key": name != "edge"}
            for name in SUPPORTED_PROVIDERS
        },
        "default_voice": DEFAULT_VOICE,
        "default_provider": "edge",
        "voices": len(EDGE_VOICES),
        "ai_gateway": get_provider_status(),
    }


def _validate_voice(voice: str, provider: str) -> str:
    """Reject an unusable voice id before any network call happens.

    This runs ahead of the request so a typo in the settings form costs nothing and
    a hostile id never reaches a URL.
    """
    candidate = (voice or "").strip()
    if not candidate:
        raise ValueError("Voice id is required.")

    if provider == "edge":
        if not VOICE_ID_PATTERN.match(candidate):
            raise ValueError(f"Invalid Edge voice id: {candidate!r}")
        if candidate not in {v.id for v in EDGE_VOICES}:
            raise ValueError(f"Unknown Edge voice id: {candidate!r}")
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
    """Swap in the provider default when the caller left our Edge default in place."""
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

    try:
        communicate = edge_tts.Communicate(text, voice, rate=rate, volume=volume, pitch=pitch)
        await communicate.save(str(output_path))
    except AuthError:
        raise
    except Exception as exc:  # edge_tts raises its own connection/audio errors
        raise AuthError(
            AUTH_REQUEST_FAILED,
            f"Edge TTS failed to synthesize the narration: {exc}",
            502,
            {"provider": "edge", "exception": type(exc).__name__},
        ) from exc

    if not output_path.exists() or output_path.stat().st_size == 0:
        raise AuthError(
            AUTH_REQUEST_FAILED,
            "Edge TTS returned no audio for the requested text.",
            502,
            {"provider": "edge", "voice": voice},
        )
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

    Raises ValueError for empty text or an unusable voice id (always before any
    network call), AuthError for a missing/rejected key or a provider failure, and
    RuntimeError when the optional edge-tts package is not installed.
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

    Renders each chunk and joins the audio. Chunks go to a temp directory next to
    the target so the join reads from the same filesystem, and the directory is
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
