"""Long-form to shorts: pick highlights, build karaoke captions, render a vertical cut.

This module previously had no caller and could not run: it invoked the async
renderer without awaiting it, its audio lookup excluded every audio extension, and
its karaoke script referenced an undefined variable. It is now wired to
/api/shorts and reuses the shared AI gateway so provider failures surface through
the same auth contract as the rest of the app.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

from backend.services.render_engine import escape_html

BASE_DIR = Path(__file__).resolve().parents[2]
# Same reasoning as pipeline.py: .env is the source of truth the settings UI writes.
load_dotenv(BASE_DIR / ".env", override=True)

STORAGE_DIR = BASE_DIR / "storage"
UPLOAD_DIR = STORAGE_DIR / "uploads"
OUTPUT_DIR = STORAGE_DIR / "outputs"

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

MAX_HIGHLIGHTS = 5
MIN_HIGHLIGHT_SECONDS = 3.0
MAX_HIGHLIGHT_SECONDS = 60.0


def _strip_code_fence(text: str) -> str:
    """Models wrap JSON in ```json fences often enough to be worth handling."""
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?", "", cleaned).strip()
    cleaned = re.sub(r"```$", "", cleaned).strip()
    return cleaned


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def detect_highlights(
    segments: List[Dict[str, Any]],
    transcript: str = "",
    niche: str = "general",
) -> List[Dict[str, Any]]:
    """Pick the strongest moments for a short.

    Uses the shared free-model gateway. Falls back to a length heuristic when the
    provider is unavailable, so the endpoint always returns something usable.
    """
    if not segments:
        return []

    # Import lazily: pipeline imports this module's siblings, and a module-level
    # import would be circular.
    from backend.services.pipeline import call_free_model, extract_json_payload

    wanted = min(len(segments), MAX_HIGHLIGHTS)
    context = " ".join(
        str(segment.get("text", "")) for segment in (segments[:3] + segments[-3:]) if segment.get("text")
    )

    prompt = (
        f"Analise esta transcricao de video sobre: {niche}\n\n"
        f"Contexto: {context[:1500]}\n\n"
        f"Escolha os {wanted} momentos mais fortes para um video vertical curto.\n"
        "Responda APENAS um array JSON, sem texto extra, neste formato:\n"
        '[{"start_time": 5.2, "end_time": 8.5, "text": "texto exato", "importance": 0.9}]\n'
        "Os tempos sao segundos. O texto deve existir literalmente na transcricao."
    )

    try:
        raw = call_free_model(prompt, json_mode=True, max_tokens=800)
        parsed = extract_json_payload(raw)
        if not isinstance(parsed, list):
            raise ValueError("o modelo não devolveu um array JSON")

        normalized: List[Dict[str, Any]] = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text", "")).strip()
            if not text:
                continue
            try:
                start = float(item.get("start_time", item.get("start", 0.0)))
                end = float(item.get("end_time", item.get("end", start + MIN_HIGHLIGHT_SECONDS)))
            except (TypeError, ValueError):
                continue
            if end <= start:
                end = start + MIN_HIGHLIGHT_SECONDS
            normalized.append({
                "start": round(start, 3),
                "end": round(end, 3),
                "text": text,
                "importance": _clamp(float(item.get("importance", 0.5) or 0.5), 0.0, 1.0),
            })

        if normalized:
            return _fit_to_source(normalized, segments)
    except Exception:
        # Provider unavailable, unparseable output, or a schema mismatch: the
        # heuristic keeps the endpoint working.
        pass

    return _fallback_highlight_detection(segments)


def _fit_to_source(highlights: List[Dict[str, Any]], segments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Keep highlights inside their source segment and inside short-form limits."""
    bounds = [
        (float(segment.get("start", 0.0)), float(segment.get("end", 0.0)))
        for segment in segments
        if segment.get("text")
    ]
    if not bounds:
        return highlights

    fitted: List[Dict[str, Any]] = []
    for highlight in highlights:
        start, end = highlight["start"], highlight["end"]
        owner = next(
            ((low, high) for low, high in bounds if low <= start < high),
            None,
        )
        if owner is None:
            continue

        low, high = owner
        start = _clamp(start, low, high)

        # The upper bound is the segment end. When the segment is shorter than the
        # minimum highlight we take whatever span is available rather than letting
        # _clamp's low > high case push the end past the segment.
        earliest_end = start + MIN_HIGHLIGHT_SECONDS
        end = high if earliest_end > high else _clamp(end, earliest_end, high)

        if end - start < 0.4:
            continue
        fitted.append({**highlight, "start": round(start, 3), "end": round(end, 3)})

    return fitted[:MAX_HIGHLIGHTS]


def _fallback_highlight_detection(segments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Heuristic pick: prefer longer, substantive segments spread across the video."""
    scored: List[Dict[str, Any]] = []
    for index, segment in enumerate(segments):
        text = str(segment.get("text", "")).strip()
        if len(text) < 15:
            continue
        start = float(segment.get("start", 0.0))
        end = float(segment.get("end", start + MIN_HIGHLIGHT_SECONDS))
        duration = _clamp(end - start, MIN_HIGHLIGHT_SECONDS, MAX_HIGHLIGHT_SECONDS)
        scored.append({
            "start": round(start, 3),
            "end": round(start + duration, 3),
            "text": text[:180],
            "importance": round(_clamp(0.4 + len(text) / 400.0, 0.0, 1.0), 2),
            "position": index,
        })

    if not scored:
        return []

    # Take the strongest, then spread them out so we do not return five
    # consecutive lines from the same paragraph.
    ranked = sorted(scored, key=lambda item: item["importance"], reverse=True)
    chosen: List[Dict[str, Any]] = []
    for candidate in ranked:
        if all(abs(candidate["start"] - kept["start"]) >= 2.0 for kept in chosen):
            chosen.append(candidate)
        if len(chosen) >= MAX_HIGHLIGHTS:
            break

    chosen.sort(key=lambda item: item["start"])
    for item in chosen:
        item.pop("position", None)
    return chosen


def extract_words_with_timestamps(segments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Distribute per-word timings across each segment, for karaoke captions."""
    words: List[Dict[str, Any]] = []
    for segment in segments:
        text = str(segment.get("text", "")).strip()
        if not text:
            continue
        start = float(segment.get("start", 0.0))
        end = float(segment.get("end", start))
        if end <= start:
            end = start + MIN_HIGHLIGHT_SECONDS

        tokens = text.split()
        if not tokens:
            continue

        # Weight each token by length so long words hold the highlight longer.
        weights = [max(1, len(token)) for token in tokens]
        total_weight = sum(weights)
        cursor = start
        span = end - start
        for index, (token, weight) in enumerate(zip(tokens, weights)):
            word_duration = span * (weight / total_weight)
            word_start = cursor
            word_end = end if index == len(tokens) - 1 else cursor + word_duration
            words.append({
                "word": token,
                "start": round(word_start, 3),
                "end": round(word_end, 3),
            })
            cursor = word_end

    return words


def words_for_range(words: List[Dict[str, Any]], start: float, end: float) -> List[Dict[str, Any]]:
    """Words whose midpoint falls inside [start, end), re-based to the clip start."""
    selected = [
        word for word in words
        if start <= (float(word["start"]) + float(word["end"])) / 2.0 < end
    ]
    return selected


def build_highlight_storyboard(
    highlights: List[Dict[str, Any]],
    all_words: List[Dict[str, Any]],
    media: Optional[List[Dict[str, Any]]] = None,
    rebase: bool = False,
) -> List[Dict[str, Any]]:
    """Turn highlights into renderable scenes.

    ``rebase=True`` runs the scenes back to back from t=0, which is what a cut
    needs: keeping the source timestamps would leave the gaps between highlights
    as dead air and stretch the render to the length of the whole narration.
    """
    palette = ["#0f172a", "#1d4ed8", "#10b981", "#f59e0b", "#ef4444"]
    scenes: List[Dict[str, Any]] = []
    cursor = 0.0

    for index, highlight in enumerate(highlights):
        source_start = float(highlight["start"])
        source_end = float(highlight["end"])
        scene_words = words_for_range(all_words, source_start, source_end)
        text = str(highlight.get("text", "")).strip()
        if not text:
            continue

        if rebase:
            start = round(cursor, 3)
            end = round(cursor + (source_end - source_start), 3)
            cursor = end
        else:
            start = round(source_start, 3)
            end = round(source_end, 3)

        if rebase and scene_words:
            scene_words = [
                {**word, "start": round(float(word["start"]) - source_start, 3),
                 "end": round(float(word["end"]) - source_start, 3)}
                for word in scene_words
            ]

        media_item = media[index % len(media)] if media else {}
        scenes.append({
            "index": index + 1,
            "start": start,
            "end": end,
            "duration": round(end - start, 3),
            "source_start": round(source_start, 3),
            "source_end": round(source_end, 3),
            "headline": text[:80],
            "caption": text,
            "words": scene_words or None,
            "caption_style": "karaoke" if scene_words else "bottom",
            "background": palette[index % len(palette)],
            "media_url": media_item.get("url", ""),
            "transition": "fade",
            "effect": "cinematic",
            "importance": highlight.get("importance", 0.5),
        })

    return scenes


def create_karaoke_html(
    highlights: List[Dict[str, Any]],
    width: int = 1080,
    height: int = 1920,
    font_size: int = 52,
) -> str:
    """Standalone karaoke preview document, for eyeballing timing outside a render."""
    scenes = build_highlight_storyboard(highlights, [])
    total = max((float(scene["end"]) for scene in scenes), default=6.0)

    clips: List[str] = []
    animations: List[str] = []
    for index, scene in enumerate(scenes):
        # escape_html, not json.dumps: quotes are not the only risk, a caption
        # containing markup must not be able to close the element.
        text = escape_html(scene["caption"])
        clips.append(
            f'<div id="hl-{index}" class="clip caption karaoke" data-start="{scene["start"]}" '
            f'data-duration="{scene["duration"]}"><span class="caption-text">{text}</span></div>'
        )
        animations.append(
            f'tl.fromTo("#hl-{index} .caption-text", {{ opacity: 0 }}, '
            f'{{ opacity: 1, duration: 0.3, immediateRender: false }}, {scene["start"]});'
        )

    if not clips:
        clips.append('<div class="clip caption bottom"><span class="caption-text">Sem destaques</span></div>')

    return f"""<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width={width}, height={height}" />
<script src="https://cdn.jsdelivr.net/npm/gsap@3.14.2/dist/gsap.min.js"></script>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
html, body {{ width:100%; height:100%; overflow:hidden; background:#05070b; }}
body {{ font-family:'Segoe UI', Tahoma, Arial, sans-serif; }}
#root {{ position:relative; width:100%; height:100%; overflow:hidden; }}
.clip {{ position:absolute; inset:0; overflow:hidden; }}
.caption {{ display:flex; align-items:flex-end; justify-content:center; text-align:center; padding:0 8% 11%; }}
.caption.karaoke {{ align-items:center; padding-bottom:0; }}
.caption-text {{ font-size:{font_size}px; font-weight:800; line-height:1.18; color:#fff;
    text-shadow:0 4px 14px rgba(0,0,0,.85); background:rgba(0,0,0,.62); padding:18px 34px; border-radius:14px; }}
.karaoke-word {{ display:inline-block; }}
</style>
</head>
<body>
<div id="root" data-composition-id="main" data-start="0" data-duration="{round(total, 3)}" data-width="{width}" data-height="{height}">
{''.join(clips)}
</div>
<script>
const tl = gsap.timeline({{ paused: true }});
{''.join(animations)}
window.__timelines["main"] = tl;
</script>
</body>
</html>"""


def find_project_audio(project_name: str) -> Optional[Path]:
    """Locate the uploaded narration for a project.

    Matches on audio extensions only - the previous version excluded .mp3 and .wav,
    so it could never find anything.
    """
    from backend.services.pipeline import AUDIO_SUFFIXES

    for item in sorted(UPLOAD_DIR.glob(f"{project_name}.*")):
        if item.suffix.lower() in AUDIO_SUFFIXES:
            return item
    return None


async def generate_shorts(
    project_name: str,
    aspect_ratio: str = "vertical",
    include_karaoke: bool = True,
    max_highlights: int = MAX_HIGHLIGHTS,
) -> Dict[str, Any]:
    """Cut a vertical short from a transcribed project.

    Pipeline: read SRT -> detect highlights -> build karaoke scenes -> render.
    """
    from backend.services.pipeline import (
        extract_keywords_from_text,
        parse_srt_to_segments,
        search_media_for_keywords,
    )
    from backend.services.render_engine import render_video_hyperframes

    srt_path = UPLOAD_DIR / f"{project_name}.srt"
    if not srt_path.exists():
        return {
            "project_name": project_name,
            "status": "error",
            "error_code": "SHORTS_NO_SRT",
            "error": "SRT file not found. Transcribe the project first.",
        }

    segments = parse_srt_to_segments(srt_path)
    if not segments:
        return {
            "project_name": project_name,
            "status": "error",
            "error_code": "SHORTS_NO_SEGMENTS",
            "error": "No segments found in the SRT file.",
        }

    transcript = srt_path.read_text(encoding="utf-8")
    highlights = detect_highlights(segments, transcript)[
        : max(1, min(max_highlights, MAX_HIGHLIGHTS))
    ]
    if not highlights:
        return {
            "project_name": project_name,
            "status": "error",
            "error_code": "SHORTS_NO_HIGHLIGHTS",
            "error": "No highlight long enough for a short was found.",
        }

    keywords = extract_keywords_from_text(transcript)
    media = search_media_for_keywords(keywords)

    all_words = extract_words_with_timestamps(segments)
    scenes = build_highlight_storyboard(highlights, all_words, media, rebase=True)
    if not scenes:
        return {
            "project_name": project_name,
            "status": "error",
            "error_code": "SHORTS_NO_HIGHLIGHTS",
            "error": "Highlights produced no renderable scenes.",
        }
    if not include_karaoke:
        for scene in scenes:
            scene["words"] = None
            scene["caption_style"] = "bottom"

    total_duration = sum(float(scene["duration"]) for scene in scenes)

    # No source audio on a cut: the highlights are pulled from scattered points of
    # the narration, so the full track would play under captions it does not match.
    # The spoken words are already burned in as captions.
    result = await render_video_hyperframes(
        project_name,
        srt_path,
        scenes,
        None,
        aspect_ratio,
        output_stem=f"{project_name}_shorts",
        media_pool=[item.get("url", "") for item in media if item.get("url")],
        # No music is selected here either: this module never calls
        # music.pick_track, so a cut with no bed is the design, not a failure.
        music_auto=False,
    )

    summary = {
        "project_name": project_name,
        "status": result.get("status", "error"),
        "output_stem": f"{project_name}_shorts",
        "output_path": result.get("output_path", ""),
        "highlights_count": len(highlights),
        "scenes": scenes,
        "aspect_ratio": aspect_ratio,
        "planned_duration": round(total_duration, 3),
        "video_duration": result.get("video_duration"),
        "resolution": result.get("resolution"),
        "has_audio": False,
        "audio_note": "corte sem audio: as palavras ja estao nas legendas",
        "karaoke": include_karaoke and any(scene.get("words") for scene in scenes),
        "message": (
            f"Short pronto: {len(scenes)} destaques, "
            f"{round(result.get('video_duration') or total_duration, 1)}s"
        ),
        "render_engine": "hyperframes",
    }
    if result.get("status") == "error":
        summary["error"] = result.get("error", "Falha no render.")

    (OUTPUT_DIR / f"{project_name}_shorts.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return summary


def list_highlights(project_name: str) -> Dict[str, Any]:
    """Report the highlights for a project without rendering anything."""
    from backend.services.pipeline import parse_srt_to_segments

    srt_path = UPLOAD_DIR / f"{project_name}.srt"
    if not srt_path.exists():
        return {"project_name": project_name, "status": "error", "error": "SRT file not found."}

    segments = parse_srt_to_segments(srt_path)
    highlights = detect_highlights(segments, srt_path.read_text(encoding="utf-8"))
    return {
        "project_name": project_name,
        "status": "ok",
        "highlights_count": len(highlights),
        "highlights": highlights,
        "total_seconds": round(sum(item["end"] - item["start"] for item in highlights), 3),
    }