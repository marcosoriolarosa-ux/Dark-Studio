
import json
import os
import re
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

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
from backend.services.viral_pipeline import render_viral_video
from backend.services.auth_contract import AuthError, to_response

app = FastAPI(title="Dark Video Studio MVP")


@app.exception_handler(AuthError)
async def auth_error_handler(request, exc: AuthError):
    """Emit the shared AUTH_* contract so the frontend handler can act on it."""
    return JSONResponse(status_code=exc.status_code, content=to_response(exc))

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:8013", "http://localhost:8013"],
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


class StrategyRequest(BaseModel):
    niche: str
    transcript: str = ""


@app.get("/health")
def health():
    return {"status": "ok", "message": "Dark Video Studio MVP running"}


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
):
    safe_name = sanitize_name(project_name.strip()) or "demo_project"
    srt_path = UPLOAD_DIR / f"{safe_name}.srt"
    if not srt_path.exists():
        raise HTTPException(status_code=400, detail="Gere primeiro a transcrição deste projeto.")

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
    render_real = await render_video_hyperframes(
        safe_name, srt_path, storyboard, audio_path, aspect_ratio,
        media_pool=pool_urls,
    )
    render_real["edit_plan"] = edit_plan
    render_real["provider_status"] = get_provider_status()

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


@app.get("/api/providers")
def get_providers():
    return {"providers": get_provider_status()}


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
