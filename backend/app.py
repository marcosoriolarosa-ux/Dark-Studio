
import inspect
import json
import os
import re
import uuid
from dataclasses import asdict, fields as dataclass_fields, is_dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from fastapi import FastAPI, UploadFile, File, Form, Body, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# Wave 1 services, imported as names rather than modules on purpose: backend.app is
# the single seam the tests patch, so mock.patch.object(app_module, "generate_script")
# is the only indirection between the API and the service layer.
from backend.services.music import (
    MOODS,
    DEFAULT_MUSIC_VOLUME,
    MUSIC_DIR,
    AUDIO_SUFFIXES as MUSIC_AUDIO_SUFFIXES,
    ensure_builtin_library,
    get_track,
    list_tracks,
    register_upload,
    search_tracks,
)
from backend.services.script_gen import (
    DEFAULT_LANGUAGE,
    LANGUAGES,
    MAX_DURATION,
    MAX_SECTIONS,
    MIN_DURATION,
    SOURCE_FALLBACK,
    generate_script,
    get_languages,
)
from backend.services.style import (
    VALID_CAPTION_MODES,
    VALID_FONT_FAMILIES,
    VALID_POSITIONS,
    list_presets,
)
from backend.services.tts import (
    DEFAULT_VOICE,
    SUPPORTED_PROVIDERS,
    get_tts_status,
    list_voices,
    synthesize_speech_long,
)
from backend.services.pipeline import (
    transcribe_audio_file,
    build_srt_from_segments,
    parse_srt_to_segments,
    build_storyboard_from_segments,
    build_edit_plan_from_segments,
    normalize_scene_timings,
    extract_keywords_from_text,
    extract_json_payload,
    search_media_for_keywords,
    search_media_for_scenes,
    fetch_provider_media,
    get_provider_status,
    call_free_model,
    UPLOAD_DIR,
    OUTPUT_DIR,
    AUDIO_SUFFIXES,
)
from backend.services.render_engine import render_video_hyperframes
from backend.services.shorts_pipeline import generate_shorts, list_highlights
from backend.services.auth_contract import AuthError, to_response
# The one-click orchestrator. Imported as a module on purpose: the job registry is
# module-level state, so every read and write has to go through the same object.
from backend.services import generator

app = FastAPI(title="Dark Video Studio MVP")


@app.exception_handler(AuthError)
async def auth_error_handler(request, exc: AuthError):
    """Emit the shared AUTH_* contract so the frontend handler can act on it."""
    return JSONResponse(status_code=exc.status_code, content=to_response(exc))

# The launcher picks a free port, so a hardcoded origin list would reject the UI
# on every machine whose port moved. DARK_STUDIO_ALLOWED_ORIGINS is a
# comma-separated override; the regex keeps every localhost port working even when
# the list is customised.
DEFAULT_ALLOWED_ORIGINS = ["http://127.0.0.1:8013", "http://localhost:8013"]
LOCALHOST_ORIGIN_REGEX = r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$"
ALLOWED_ORIGINS_ENV = "DARK_STUDIO_ALLOWED_ORIGINS"


def allowed_origins() -> List[str]:
    """Origins from the environment, falling back to the historical pair."""
    raw = os.getenv(ALLOWED_ORIGINS_ENV, "")
    origins = [item.strip() for item in raw.split(",") if item.strip()]
    return origins or list(DEFAULT_ALLOWED_ORIGINS)


app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins(),
    allow_origin_regex=LOCALHOST_ORIGIN_REGEX,
    allow_methods=["*"],
    allow_headers=["*"],
)


def sanitize_name(name: str) -> str:
    return re.sub(r'[^a-zA-Z0-9_-]', '', name)

FRONTEND_DIR = Path(__file__).resolve().parents[1] / "frontend"
if FRONTEND_DIR.exists():
    app.mount("/app", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")


class ProjectRequest(BaseModel):
    project_name: str = "demo_project"


class ApiSettings(BaseModel):
    openrouter_api_key: str = ""
    openrouter_model: str = "meta-llama/llama-3.3-8b-instruct:free"
    pexels_api_key: str = ""
    pixabay_api_key: str = ""
    gemini_api_key: str = ""
    openai_api_key: str = ""
    youtube_api_key: str = ""
    azure_speech_key: str = ""
    azure_speech_region: str = ""


class StrategyRequest(BaseModel):
    niche: str
    transcript: str = ""


class ScriptRequest(BaseModel):
    topic: str
    # Optional: when present the script is stored next to the project so
    # POST /api/tts can narrate it without the UI resending the whole text.
    project_name: str = ""
    language: str = DEFAULT_LANGUAGE
    section_count: int = 5
    tone: str = "documentary"
    duration_target: int = 60
    custom_instructions: str = ""


class TtsRequest(BaseModel):
    project_name: str = "demo_project"
    # Empty means "narrate the script stored for this project".
    text: str = ""
    voice: str = DEFAULT_VOICE
    provider: str = "edge"
    rate: str = "+0%"
    volume: str = "+0%"
    pitch: str = "+0Hz"


SCRIPT_SUFFIX = ".script.json"

# Narration pace, used for the estimate only; same figure as script_gen uses.
WORDS_PER_SECOND = 2.5

# Cap on a user music upload. The read is bounded so a hostile Content-Length
# cannot turn into a huge allocation before the check runs.
MAX_MUSIC_UPLOAD_BYTES = 20 * 1024 * 1024


def _script_path(safe_name: str) -> Path:
    return UPLOAD_DIR / f"{safe_name}{SCRIPT_SUFFIX}"


def _store_script(safe_name: str, payload: Dict[str, Any]) -> bool:
    """Persist a generated script for later narration. Never raises."""
    try:
        _script_path(safe_name).write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
        return True
    except (OSError, TypeError, ValueError):
        return False


def _stored_script_text(safe_name: str) -> str:
    """Narration text of the stored script, or "" when there is none.

    Falls back to hook + sections when ``full_text`` is missing, and keeps the hook
    because it is meant to be the first spoken line.
    """
    path = _script_path(safe_name)
    if not path.exists():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if not isinstance(data, dict):
        return ""
    full_text = str(data.get("full_text") or "").strip()
    if full_text:
        return full_text
    parts: List[str] = []
    hook = str(data.get("hook") or "").strip()
    if hook:
        parts.append(hook)
    for section in data.get("sections") or []:
        if isinstance(section, dict):
            text = str(section.get("text") or "").strip()
            if text:
                parts.append(text)
    return "\n\n".join(parts).strip()


def _parse_bool(value: Any, default: bool) -> bool:
    """Form booleans arrive as strings; anything unrecognised keeps the default."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        folded = value.strip().casefold()
        if folded in ("true", "1", "yes", "on"):
            return True
        if folded in ("false", "0", "no", "off"):
            return False
    return default


def _parse_float(value: Any, default: float, low: float, high: float) -> Optional[float]:
    """Clamp a numeric form field, or None when it is not a number at all."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN cannot survive a comparison
        return None
    return max(low, min(high, number))


def _voice_payload(voice: Any) -> Dict[str, Any]:
    """Serialise a VoiceInfo, tolerating a mapping or a bare id."""
    if is_dataclass(voice) and not isinstance(voice, type):
        return asdict(voice)
    if isinstance(voice, Mapping):
        return dict(voice)
    return {
        "id": str(voice),
        "name": str(voice),
        "gender": "unknown",
        "locale": "",
        "provider": "edge",
    }


def _track_payload(track: Any) -> Dict[str, Any]:
    if is_dataclass(track) and not isinstance(track, type):
        return asdict(track)
    if isinstance(track, Mapping):
        return dict(track)
    return {"id": str(track)}


def _renderer_accepts(renderer: Any, keyword: str) -> bool:
    """Whether the renderer takes `keyword`, so an older engine still renders.

    Inspected rather than caught: a TypeError raised *inside* the renderer would
    otherwise trigger a second, partial render. A mock exposes a
    ``(*args, **kwargs)`` signature, so a patched renderer receives the full set
    of keywords and the tests can assert them exactly.
    """
    try:
        parameters = inspect.signature(renderer).parameters
    except (TypeError, ValueError):
        return True
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values()):
        return True
    return keyword in parameters


@app.get("/health")
def health():
    return {"status": "ok", "message": "Dark Video Studio MVP running"}


@app.get("/api/languages")
def list_languages():
    """Narration languages the script generator supports."""
    try:
        return {"languages": get_languages(), "default": DEFAULT_LANGUAGE}
    except Exception as exc:  # the picker must render even if the service breaks
        return {"languages": [], "default": DEFAULT_LANGUAGE, "error": str(exc)}


@app.post("/api/script")
def create_script(request: ScriptRequest):
    """Turn a topic into a narration script.

    Synchronous on purpose: ``script_gen.generate_script`` is a sync function and
    wrapping it in ``async def`` would block the event loop on the model call.
    """
    if not request.topic.strip():
        raise HTTPException(status_code=400, detail="Indique um tema para gerar o guião.")
    language = (request.language or "").strip()
    if language not in LANGUAGES:
        raise HTTPException(status_code=400, detail="Idioma de narração não suportado.")
    if request.section_count < 1 or request.section_count > MAX_SECTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"O número de secções tem de estar entre 1 e {MAX_SECTIONS}.",
        )
    duration = max(MIN_DURATION, min(int(request.duration_target), MAX_DURATION))

    try:
        script = generate_script(
            request.topic,
            language=language,
            section_count=request.section_count,
            tone=request.tone,
            duration_target=duration,
            custom_instructions=request.custom_instructions,
        )
    except ValueError as exc:
        # Blank topic / out-of-range section count, the only two the service raises.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=422, detail=f"Não foi possível gerar o guião: {exc}"
        ) from exc

    payload = script.to_dict()
    # A local script is a success, not an error, but the degradation has to be
    # visible: "fallback-local" plus a non-null fallback_error say why.
    payload["degraded"] = script.source == SOURCE_FALLBACK

    safe_name = sanitize_name(request.project_name.strip())
    if safe_name:
        payload["project_name"] = safe_name
        payload["stored"] = _store_script(safe_name, payload)
    return payload


@app.get("/api/voices")
def get_voices(locale: str = ""):
    """Voice catalogue, optionally narrowed to a locale prefix.

    A locale with no match is an empty list, not an error: the picker simply
    shows nothing for that language.
    """
    try:
        voices = [_voice_payload(item) for item in list_voices(locale)]
    except Exception:
        voices = []
    return {
        "voices": voices,
        "default": DEFAULT_VOICE,
        "locale": (locale or "").strip(),
        "providers": list(SUPPORTED_PROVIDERS),
    }


@app.get("/api/tts/status")
def tts_status():
    """Which narration providers can run right now.

    Always 200: a missing optional package is a degraded capability the settings
    panel renders, not a failed request.
    """
    try:
        status = get_tts_status()
        if not isinstance(status, dict):
            raise ValueError("estado de TTS inesperado")
        return status
    except Exception as exc:
        return {
            "providers": {
                name: {"available": False, "requires_key": name != "edge"}
                for name in SUPPORTED_PROVIDERS
            },
            "default_voice": DEFAULT_VOICE,
            "default_provider": "edge",
            "voices": 0,
            "ai_gateway": {},
            "error": str(exc),
        }


@app.post("/api/tts")
async def synthesize_narration(request: TtsRequest):
    """Narrate a project and leave the audio + SRT the build-video flow expects."""
    safe_name = sanitize_name(request.project_name.strip()) or "demo_project"

    provider = (request.provider or "edge").strip().lower()
    if provider not in SUPPORTED_PROVIDERS:
        raise HTTPException(status_code=400, detail="Fornecedor de voz não suportado.")

    text = (request.text or "").strip()
    if not text:
        text = _stored_script_text(safe_name)
    if not text:
        raise HTTPException(
            status_code=400,
            detail="Não há texto para narrar: envie `text` ou gere um guião primeiro.",
        )

    audio_path = UPLOAD_DIR / f"{safe_name}.mp3"
    try:
        await synthesize_speech_long(
            text,
            audio_path,
            voice=request.voice,
            provider=provider,
            rate=request.rate,
            volume=request.volume,
            pitch=request.pitch,
        )
    except AuthError:
        # Missing/rejected key, rate limit, upstream failure: re-raised so the
        # shared handler emits the real status and the AUTH_* contract.
        raise
    except ValueError as exc:
        # Bad voice id or empty text, both raised before any network call.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        # edge-tts not installed, ffmpeg missing: actionable, not a user error.
        raise HTTPException(
            status_code=422, detail=f"Não foi possível gerar a narração: {exc}"
        ) from exc

    try:
        segments = transcribe_audio_file(audio_path)
        srt_file = UPLOAD_DIR / f"{safe_name}.srt"
        srt_file.write_text(build_srt_from_segments(segments), encoding="utf-8")
    except HTTPException:
        raise
    except Exception as exc:
        # whisper/ffmpeg could not read the generated audio, so there are no
        # usable subtitles; /api/build-video would fail later without them.
        raise HTTPException(
            status_code=422, detail=f"Não foi possível gerar as legendas: {exc}"
        ) from exc

    return {
        "project_name": safe_name,
        "audio_file": str(audio_path),
        "srt_file": str(srt_file),
        "segments": segments,
        "voice": request.voice,
        "provider": provider,
        "characters": len(text),
        "estimated_seconds": round(len(text.split()) / WORDS_PER_SECOND, 2),
    }


@app.get("/api/presets")
def get_presets():
    """Theme presets plus the closed vocabularies the caption controls offer."""
    try:
        presets = [preset.to_dict() for preset in list_presets()]
    except Exception:
        presets = []
    return {
        "presets": presets,
        "positions": list(VALID_POSITIONS),
        "modes": list(VALID_CAPTION_MODES),
        "fonts": list(VALID_FONT_FAMILIES),
    }


@app.get("/api/music/tracks")
def get_music_tracks(mood: str = ""):
    """Music library, optionally filtered by mood."""
    wanted = (mood or "").strip()
    if wanted and wanted not in MOODS:
        raise HTTPException(status_code=400, detail="Ambiente musical não suportado.")
    try:
        # storage/music/ is gitignored, so a fresh checkout holds no WAVs at all
        # and a pure read here answers []: an empty picker until some unrelated
        # job happens to generate the library. Healing it at the endpoint is what
        # pick_track does for auto-BGM. list_tracks itself stays a pure read on
        # purpose, because it also answers "does this id exist?", where writing a
        # file would be a lie. Idempotent: once the library is on disk this costs
        # one stat per spec.
        ensure_builtin_library()
        tracks = [_track_payload(item) for item in list_tracks(wanted)]
    except Exception:
        tracks = []
    return {"tracks": tracks, "moods": list(MOODS), "mood": wanted}


@app.get("/api/music/search")
def search_music(q: str = ""):
    query = (q or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="Indique um termo de pesquisa.")
    try:
        # Same defect as GET /api/music/tracks: searching an unmaterialised
        # library finds nothing, so the picker looks broken on a fresh checkout.
        # search_tracks stays a pure read over list_tracks; the self-heal is the
        # endpoint's job.
        ensure_builtin_library()
        tracks = [_track_payload(item) for item in search_tracks(query)]
    except Exception:
        tracks = []
    return {"tracks": tracks, "query": query}


@app.post("/api/music/upload")
async def upload_music(
    file: UploadFile = File(...),
    title: str = Form(""),
    mood: str = Form("ambient"),
):
    """Register a user audio file in the music library."""
    filename = file.filename or ""
    suffix = Path(filename).suffix.lower()
    if suffix not in MUSIC_AUDIO_SUFFIXES:
        raise HTTPException(
            status_code=400, detail="Formato de áudio não suportado para música."
        )

    wanted_mood = (mood or "").strip() or "ambient"
    if wanted_mood not in MOODS:
        raise HTTPException(status_code=400, detail="Ambiente musical não suportado.")

    content = await file.read(MAX_MUSIC_UPLOAD_BYTES + 1)
    if len(content) > MAX_MUSIC_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Ficheiro de música demasiado grande.")
    if not content:
        raise HTTPException(status_code=400, detail="Ficheiro de música vazio.")

    # Written under a random name: the caller's filename never reaches the
    # filesystem, and register_upload still gets to slugify the title itself.
    staged = UPLOAD_DIR / f"music_upload_{uuid.uuid4().hex}{suffix}"
    try:
        staged.write_bytes(content)
    except OSError as exc:
        raise HTTPException(
            status_code=422, detail="Não foi possível guardar o ficheiro de música."
        ) from exc

    try:
        track = register_upload(staged, title=title, mood=wanted_mood)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=422, detail="Não foi possível registar a música: " + str(exc)
        ) from exc
    finally:
        try:
            staged.unlink(missing_ok=True)
        except OSError:
            pass

    payload = _track_payload(track)
    stored_path = Path(str(payload.get("path") or ""))
    try:
        inside = stored_path.resolve().is_relative_to(MUSIC_DIR.resolve())
    except (OSError, ValueError):
        inside = False
    if not inside:
        # register_upload slugifies, so this should be unreachable; refusing here
        # keeps a future change in that slugifier from becoming a path traversal.
        raise HTTPException(
            status_code=400, detail="Nome de faixa inválido: o título não pode conter caminhos."
        )
    return {"status": "ok", "track": payload}


@app.post("/api/transcribe")
async def transcribe_audio(
    file: UploadFile = File(...),
    project_name: str = Form("demo_project"),
):
    safe_name = sanitize_name(project_name.strip()) or "demo_project"
    suffix = file.filename[file.filename.rfind('.'):] if '.' in file.filename else '.mp3'
    file_path = UPLOAD_DIR / f"{safe_name}{suffix}"
    content = await file.read()
    file_path.write_bytes(content)

    segments = transcribe_audio_file(file_path)
    srt_text = build_srt_from_segments(segments)
    srt_file = UPLOAD_DIR / f"{safe_name}.srt"
    srt_file.write_text(srt_text, encoding="utf-8")

    return {
        "project_name": safe_name,
        "audio_file": str(file_path),
        "srt_file": str(srt_file),
        "segments": segments,
    }


@app.post("/api/build-video")
async def build_video(
    project_name: str = Form("demo_project"),
    transition: str = Form("fade"),
    effect: str = Form("cinematic"),
    include_captions: str = Form("true"),
    storyboard_json: str = Form(""),
    aspect_ratio: str = Form("vertical"),
    topic: str = Form(""),
    preset: str = Form(""),
    subtitle_style: str = Form(""),
    music_track: str = Form(""),
    music_volume: str = Form(""),
    duck_voice: str = Form("true"),
):
    safe_name = sanitize_name(project_name.strip()) or "demo_project"
    srt_path = UPLOAD_DIR / f"{safe_name}.srt"
    if not srt_path.exists():
        raise HTTPException(status_code=400, detail="Gere primeiro a transcrição deste projeto.")

    # Parsed here rather than inside the renderer so a malformed payload is a 400
    # with a readable message instead of a failed render.
    style_payload = None
    if subtitle_style.strip():
        try:
            style_payload = json.loads(subtitle_style)
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=400, detail="Estilo de legendas inválido: JSON malformado."
            ) from exc
        if not isinstance(style_payload, dict):
            raise HTTPException(
                status_code=400, detail="Estilo de legendas tem de ser um objecto JSON."
            )

    volume = _parse_float(music_volume, DEFAULT_MUSIC_VOLUME, 0.0, 1.0)
    if volume is None:
        raise HTTPException(
            status_code=400, detail="O volume da música tem de ser um número entre 0 e 1."
        )
    duck = _parse_bool(duck_voice, True)

    audio_path = next(
        (item for item in sorted(UPLOAD_DIR.glob(f"{safe_name}.*")) if item.suffix.lower() in AUDIO_SUFFIXES),
        None,
    )
    raw_text = srt_path.read_text(encoding="utf-8")
    keywords = extract_keywords_from_text(raw_text)

    segments = parse_srt_to_segments(srt_path) or [
        {"index": i + 1, "start": i * 3.0, "end": (i + 1) * 3.0, "text": phrase}
        for i, phrase in enumerate(keywords)
    ]
    storyboard = build_storyboard_from_segments(segments)
    if storyboard_json.strip():
        try:
            custom_storyboard = json.loads(storyboard_json)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="Storyboard inválido.") from exc
        if not isinstance(custom_storyboard, list):
            raise HTTPException(status_code=400, detail="Storyboard deve ser uma lista de cenas.")
        if not all(isinstance(scene, dict) for scene in custom_storyboard):
            raise HTTPException(status_code=400, detail="Cada cena do storyboard deve ser um objecto.")
        # A storyboard enviada pela UI também tem de ficar sequencial.
        storyboard = normalize_scene_timings(custom_storyboard)

    # Media is searched per scene so each scene's imagery follows its own words,
    # rather than every scene cycling through one global list.
    scene_media, term_source = search_media_for_scenes(storyboard, niche=topic.strip())
    global_media = search_media_for_keywords(keywords)
    media_pool = global_media or [item for items in scene_media.values() for item in items]

    for idx, scene in enumerate(storyboard):
        candidates = scene_media.get(idx) or []
        media_item = candidates[0] if candidates else {}
        scene["media_url"] = scene.get("media_url") or media_item.get("url", "")
        scene["search_terms"] = [item.get("keyword", "") for item in candidates[:3]]
        scene["transition"] = transition
        scene["effect"] = effect
        scene["caption_style"] = "bottom" if include_captions.lower() == "true" else "hidden"
        scene["provider"] = media_item.get("source", "none")

    edit_plan = build_edit_plan_from_segments(segments, media_pool)
    for idx, scene in enumerate(edit_plan):
        candidates = scene_media.get(idx) or []
        scene["transition"] = transition
        scene["effect"] = effect
        scene["captions"] = include_captions.lower() == "true"
        scene["search_terms"] = [item.get("keyword", "") for item in candidates[:3]]
        if candidates:
            scene["media_url"] = candidates[0].get("url", "")

    audio_path = next(
        (item for item in sorted(UPLOAD_DIR.glob(f"{safe_name}.*")) if item.suffix.lower() in AUDIO_SUFFIXES),
        None,
    )
    pool_urls = [item.get("url", "") for item in media_pool if item.get("url")]
    wanted_render_kwargs = {
        "media_pool": pool_urls,
        "preset": preset.strip(),
        "subtitle_style": style_payload,
        "music_track": music_track.strip(),
        "music_volume": volume,
        "duck_voice": duck,
    }
    render_kwargs = {
        name: value
        for name, value in wanted_render_kwargs.items()
        if _renderer_accepts(render_video_hyperframes, name)
    }
    render_real = await render_video_hyperframes(
        safe_name, srt_path, storyboard, audio_path, aspect_ratio,
        **render_kwargs,
    )
    render_real["edit_plan"] = edit_plan
    render_real["provider_status"] = get_provider_status()

    track_info = get_track(music_track.strip()) if music_track.strip() else None
    return {
        "project_name": safe_name,
        "keywords": keywords,
        "media": media_pool,
        "scene_media": {str(idx): items for idx, items in scene_media.items() if items},
        "segments": segments,
        "storyboard": storyboard,
        "edit_plan": edit_plan,
        "scene_count": len(storyboard),
        "scenes_with_media": sum(1 for scene in storyboard if scene.get("media_url")),
        "term_source": term_source,
        "provider_status": get_provider_status(),
        "render": render_real,
        "render_real": render_real,
        # Echoed so the UI can show what was actually applied to the render.
        "preset": preset.strip(),
        "subtitle_style": style_payload,
        "music_track": music_track.strip(),
        "music": _track_payload(track_info) if track_info is not None else None,
        "music_volume": volume,
        "duck_voice": duck,
    }


@app.post("/api/build-viral")
async def build_viral(
    project_name: str = Form("viral_project"),
    aspect_ratio: str = Form("vertical"),
    include_karaoke: str = Form("true"),
    topic: str = Form(""),
    niche: str = Form(""),
):
    safe_name = sanitize_name(project_name.strip()) or "viral_project"
    srt_path = UPLOAD_DIR / f"{safe_name}.srt"
    if not srt_path.exists():
        raise HTTPException(status_code=400, detail="Gere primeiro a transcrição deste projeto.")

    # Imported here, not at module scope: viral_pipeline needs librosa, and a
    # top-level import made the entire app fail to start without it.
    try:
        from backend.services.viral_pipeline import render_viral_video
    except ImportError as exc:
        raise HTTPException(
            status_code=503,
            detail=(
                "O pipeline viral precisa do pacote opcional 'librosa'. "
                "Instale-o com 'pip install -r requirements.txt'."
            ),
        ) from exc

    result = await render_viral_video(
        safe_name, srt_path, aspect_ratio,
        include_karaoke=include_karaoke.lower() != "false",
        topic=topic.strip(),
        niche=niche.strip(),
    )

    if result.get("status") == "error":
        raise HTTPException(status_code=422, detail=result.get("error", "Falha no render viral."))

    return {
        "project_name": safe_name,
        "status": result.get("status"),
        "output_path": result.get("output_path"),
        "video_duration": result.get("video_duration"),
        "resolution": result.get("resolution"),
        "aspect_ratio": aspect_ratio,
        "beat_data": result.get("viral_meta", {}).get("beat_data", {}),
        "platform_metadata": result.get("viral_meta", {}).get("platform_metadata", {}),
        "thumbnail_path": result.get("viral_meta", {}).get("thumbnail_path"),
        "loop_duration": result.get("viral_meta", {}).get("loop_duration"),
        "hook_duration": result.get("viral_meta", {}).get("hook_duration"),
        "scenes_with_media": result.get("scenes_with_media"),
        "storyboard": result.get("viral_meta", {}).get("storyboard", []),
        "render_engine": "hyperframes",
        "message": "Vídeo viral pronto: beat-sync, loop contínuo, thumbnail otimizado, metadados de plataforma.",
    }


# ------------------------------------------------------------ one-click generation
# GenerationRequest is the single source of truth for what a job accepts, so the
# allowed key set is read off the dataclass at import time rather than written
# out by hand: a new field is picked up automatically and a renamed one cannot
# leave a stale literal behind.
GENERATION_PARAM_FIELDS: frozenset[str] = frozenset(
    field.name for field in dataclass_fields(generator.GenerationRequest)
)


def _generation_params(payload: Any) -> Dict[str, Any]:
    """Keep only the keys GenerationRequest declares, dropping the rest.

    submit_job does ``GenerationRequest(**params)``, so a single stray key from an
    older or hand-rolled client (mood, project_name, ...) would come back as a
    TypeError. Filtering here makes those requests run on defaults instead of
    failing, which is the difference between a 202 and a 400 for a client we do
    not control.
    """
    if not isinstance(payload, Mapping):
        return {}
    return {
        key: value
        for key, value in payload.items()
        if key in GENERATION_PARAM_FIELDS
    }


@app.post("/api/generate", status_code=202)
async def submit_generation(payload: Dict[str, Any] = Body(default_factory=dict)):
    """Queue a whole topic -> video run and answer immediately with the job.

    The body is taken raw instead of through a Pydantic model on purpose: the
    dataclass in generator.py is the real contract, and a model would either
    duplicate it or turn a missing topic into a 422 before the Portuguese
    message can be shown.

    async def on purpose: submit_job schedules the pipeline on the running event
    loop, so a sync route would run it in a threadpool with no loop and the job
    would sit queued forever. The reply is the only thing this call produces - the
    client polls GET /api/jobs/{job_id} for progress.
    """
    try:
        job = generator.submit_job(_generation_params(payload))
    except ValueError as exc:
        # Blank topic, unknown preset, out-of-range section count: a user
        # mistake, reported with the Portuguese message the service produced.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        # Anything else is our bug, not the caller. Say so in pt-PT instead of
        # leaking a bare traceback-shaped body.
        raise HTTPException(
            status_code=500, detail=f"Falha ao enfileirar a geração: {exc}"
        ) from exc
    return JSONResponse(status_code=202, content=job.to_dict())


@app.get("/api/jobs")
def list_generation_jobs():
    """Every known job as a bare list, newest first (the history panel polls this)."""
    return JSONResponse(content=[job.to_dict() for job in generator.list_jobs()])


@app.get("/api/jobs/{job_id}")
def get_generation_job(job_id: str):
    job = generator.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job não encontrado.")
    return JSONResponse(content=job.to_dict())


@app.delete("/api/jobs/{job_id}")
def cancel_generation_job(job_id: str):
    """Cancel a job that has not started yet; a running one always reports false.

    The 404 is separate from the cancel result on purpose: an unknown id is a
    client bug, a job that is already running is a documented limitation.
    """
    if generator.get_job(job_id) is None:
        raise HTTPException(status_code=404, detail="Job não encontrado.")
    return {"cancelled": generator.cancel_job(job_id)}


@app.get("/api/providers")
def get_providers():
    """Media/AI provider status, plus the narration providers for the same panel.

    Guarded because it is the endpoint the settings page calls on load: an
    optional dependency going missing must degrade the payload, not 500 it.
    """
    try:
        providers = get_provider_status()
    except Exception as exc:
        providers = {"error": str(exc)}
    try:
        tts = get_tts_status()
    except Exception as exc:
        tts = {
            "providers": {
                name: {"available": False, "requires_key": name != "edge"}
                for name in SUPPORTED_PROVIDERS
            },
            "default_voice": DEFAULT_VOICE,
            "error": str(exc),
        }
    return {"providers": providers, "tts": tts}


@app.post("/api/strategy")
def generate_strategy(request: StrategyRequest):
    niche = request.niche.strip()
    if not niche:
        raise HTTPException(status_code=400, detail="Indique um tema ou nicho.")
    transcript = request.transcript.strip() if request.transcript else ""
    prompt = """Analise o tema de vídeo: {niche}
Texto disponível: {transcript}
Responda APENAS JSON neste formato exato:
{{"angle":"...","hook":"...","titles":["...","...","..."],"visual_direction":"...","chapters":["...","...","..."]}}
Não invente métricas nem diga que os dados são reais.""".format(niche=niche, transcript=transcript[:4000])
    try:
        raw = call_free_model(prompt, json_mode=True, max_tokens=600)
        parsed = extract_json_payload(raw)
        if not isinstance(parsed, dict):
            raise ValueError("o modelo não devolveu um objecto JSON")
        required = {"angle", "hook", "titles", "visual_direction", "chapters"}
        missing = required - {str(key).lower() for key in parsed}
        if missing:
            raise ValueError(f"Campos obrigatórios faltando: {sorted(missing)}")
        if not isinstance(parsed.get("titles"), list) or len(parsed["titles"]) < 3:
            raise ValueError("'titles' deve ser uma array com pelo menos 3 itens")
        if not isinstance(parsed.get("chapters"), list) or len(parsed["chapters"]) < 3:
            raise ValueError("'chapters' deve ser uma array com pelo menos 3 itens")
        parsed["source"] = "openrouter-free"
        parsed["model"] = get_provider_status()["openrouter"]["model"]
        return parsed
    except (json.JSONDecodeError, ValueError, RuntimeError) as e:
        return {
            "angle": f"A história invisível por trás de {niche}",
            "hook": f"O que quase ninguém percebe sobre {niche}",
            "titles": [
                f"A verdade escondida sobre {niche}",
                f"Como {niche} mudou sem ninguém notar",
                f"O ponto fraco invisível de {niche}",
            ],
            "visual_direction": "Documentário cinematográfico, mapas, detalhes macro, cortes a cada 3-5 segundos e legendas fortes.",
            "chapters": ["O gancho", "A escala do problema", "Como funciona", "A revelação", "O que acontece agora"],
            "source": "fallback-local",
            "fallback_error": str(e),
        }


@app.post("/api/settings")
def save_api_settings(settings: ApiSettings):
    if settings.openrouter_model and not settings.openrouter_model.endswith(":free"):
        raise HTTPException(status_code=400, detail="Use apenas um modelo OpenRouter terminado em :free.")
    values = {
        "OPENROUTER_API_KEY": settings.openrouter_api_key.strip(),
        "OPENROUTER_MODEL": settings.openrouter_model.strip() or "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
        "PEXELS_API_KEY": settings.pexels_api_key.strip(),
        "PIXABAY_API_KEY": settings.pixabay_api_key.strip(),
        "GEMINI_API_KEY": settings.gemini_api_key.strip() or os.getenv("GEMINI_API_KEY", ""),
        "OPENAI_API_KEY": settings.openai_api_key.strip() or os.getenv("OPENAI_API_KEY", ""),
        "YOUTUBE_API_KEY": settings.youtube_api_key.strip() or os.getenv("YOUTUBE_API_KEY", ""),
        # Azure speech needs both halves; they are stored side by side so a saved
        # key is never a regionless credential every request would reject.
        "AZURE_SPEECH_KEY": settings.azure_speech_key.strip() or os.getenv("AZURE_SPEECH_KEY", ""),
        "AZURE_SPEECH_REGION": settings.azure_speech_region.strip() or os.getenv("AZURE_SPEECH_REGION", ""),
    }
    env_path = Path(__file__).resolve().parents[1] / ".env"
    env_path.write_text("\n".join(f"{key}={value}" for key, value in values.items()) + "\n", encoding="utf-8")
    for key, value in values.items():
        if value:
            os.environ[key] = value
        else:
            os.environ.pop(key, None)
    return {"status": "saved", "providers": get_provider_status()}


@app.post("/api/ai/test")
def test_ai_provider():
    try:
        answer = call_free_model("Responda apenas: OK")
        return {
            "status": "ok",
            "provider": "openrouter",
            "model": get_provider_status()["openrouter"]["model"],
            "response": answer[:80],
        }
    except AuthError as exc:
        # Surface the contract instead of flattening it to a generic error string.
        return JSONResponse(status_code=exc.status_code, content={"status": "error", **to_response(exc)})
    except (RuntimeError, ValueError) as exc:
        return {
            "status": "error",
            "provider": "openrouter",
            "model": get_provider_status()["openrouter"]["model"],
            "error": str(exc),
        }


@app.get("/api/media/search")
def search_media(query: str, provider: str = "pexels"):
    return {"query": query, "provider": provider, "results": fetch_provider_media(query, provider)}


@app.get("/api/project/{project_name}/video")
def get_project_video(project_name: str):
    safe_name = sanitize_name(project_name)
    if not safe_name:
        raise HTTPException(status_code=400, detail="Nome de projeto inválido.")
    file_path = OUTPUT_DIR / f"{safe_name}.mp4"
    if not file_path.exists():
        # 404, not 200-with-JSON: a <video> element cannot read an error body and
        # would fail silently in the player.
        raise HTTPException(status_code=404, detail="Vídeo ainda não foi gerado.")
    return FileResponse(file_path, media_type="video/mp4")


@app.get("/api/project/{project_name}/shorts/video")
def get_project_shorts_video(project_name: str):
    safe_name = sanitize_name(project_name)
    if not safe_name:
        raise HTTPException(status_code=400, detail="Nome de projeto inválido.")
    file_path = OUTPUT_DIR / f"{safe_name}_shorts.mp4"
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Short ainda não foi gerado.")
    return FileResponse(file_path, media_type="video/mp4")


@app.get("/api/projects")
def list_projects():
    files = []
    for item in sorted(UPLOAD_DIR.iterdir()):
        if item.is_file():
            files.append({"name": item.name, "path": str(item)})
    return {"projects": files}


@app.get("/api/shorts/{project_name}/highlights")
def get_short_highlights(project_name: str):
    safe_name = sanitize_name(project_name)
    if not safe_name:
        raise HTTPException(status_code=400, detail="Nome de projeto inválido.")
    return list_highlights(safe_name)


@app.post("/api/shorts")
async def build_shorts(
    project_name: str = Form("demo_project"),
    aspect_ratio: str = Form("vertical"),
    include_karaoke: str = Form("true"),
    max_highlights: int = Form(3),
):
    safe_name = sanitize_name(project_name.strip()) or "demo_project"
    max_highlights = max(1, min(int(max_highlights), 5))
    summary = await generate_shorts(
        safe_name,
        aspect_ratio=aspect_ratio if aspect_ratio in {"vertical", "square", "landscape"} else "vertical",
        include_karaoke=include_karaoke.lower() != "false",
        max_highlights=max_highlights,
    )
    if summary.get("status") == "error":
        status_code = 404 if summary.get("error_code") in {"SHORTS_NO_SRT", "SHORTS_NO_SEGMENTS"} else 422
        raise HTTPException(status_code=status_code, detail=summary.get("error", "Falha ao gerar o short."))
    return summary
