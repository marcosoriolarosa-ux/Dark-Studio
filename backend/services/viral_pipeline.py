"""Viral-ready video pipeline: beat-sync cuts, seamless loop, thumbnail, platform metadata."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import librosa
import numpy as np

from backend.services.pipeline import (
    UPLOAD_DIR,
    OUTPUT_DIR,
    AUDIO_SUFFIXES,
    parse_srt_to_segments,
    build_storyboard_from_segments,
    normalize_scene_timings,
    extract_keywords_from_text,
    search_media_for_scenes,
    search_media_for_keywords,
    build_edit_plan_from_segments,
    get_provider_status,
)
from backend.services.render_engine import render_video_hyperframes, get_dimensions

BASE_DIR = Path(__file__).resolve().parents[2]
STORAGE_DIR = BASE_DIR / "storage"
THUMB_DIR = STORAGE_DIR / "thumbnails"
THUMB_DIR.mkdir(parents=True, exist_ok=True)

LOOP_DURATION = 0.5
BEAT_SYNC_TOLERANCE = 0.15
MIN_SCENE_DURATION = 0.8
VIRAL_HOOK_SECONDS = 3.0


def detect_beats(audio_path: Path) -> Dict[str, Any]:
    """Detect beats and tempo using librosa."""
    try:
        y, sr = librosa.load(str(audio_path), sr=None)
        tempo, beat_frames = librosa.beat.beat_track(y=y, sr=sr, units="time")
        beats = beat_frames.tolist()
        # librosa.beat.beat_track returns tempo as a 1-element ndarray in newer
        # versions (numpy 2.x with librosa 1.0.0), not a scalar float, so a plain
        # float(tempo) raises TypeError. atleast_1d handles both scalar and array.
        tempo_value = float(np.atleast_1d(tempo)[0])
        return {
            "tempo": tempo_value,
            "beats": beats,
            "beat_count": len(beats),
            "duration": float(len(y) / sr),
        }
    except Exception as e:
        return {
            "tempo": 120.0,
            "beats": [],
            "beat_count": 0,
            "duration": 0.0,
            "error": str(e),
        }


def snap_to_nearest_beat(time: float, beats: List[float], tolerance: float = BEAT_SYNC_TOLERANCE) -> float:
    """Snap a timestamp to the nearest beat within tolerance."""
    if not beats:
        return time
    nearest = min(beats, key=lambda b: abs(b - time))
    if abs(nearest - time) <= tolerance:
        return round(nearest, 3)
    return round(time, 3)


def force_snap_to_beat(time: float, beats: List[float]) -> float:
    """Force snap to nearest beat (no tolerance) for viral cut precision."""
    if not beats:
        return time
    nearest = min(beats, key=lambda b: abs(b - time))
    return round(nearest, 3)


def build_beat_synced_storyboard(
    segments: List[Dict[str, Any]],
    beats: List[float],
    aspect_ratio: str = "vertical",
) -> List[Dict[str, Any]]:
    """Build storyboard with cuts snapped to beats, first scene = hook (<=3s)."""
    if not segments:
        return []

    # Build storyboard WITHOUT normalization first (preserve original SRT times)
    palette = ["#0f172a", "#1d4ed8", "#10b981", "#f59e0b", "#ef4444", "#a855f7"]
    raw_scenes: List[Dict[str, Any]] = []
    for segment in segments:
        text = str(segment.get("text", "")).strip()
        if not text:
            continue
        start = float(segment.get("start", 0.0))
        end = float(segment.get("end", start))
        headline = text[:80].strip()
        short_text = text if len(text) <= 80 else text[:77].rstrip() + "..."
        raw_scenes.append({
            "index": int(segment.get("index", len(raw_scenes) + 1)),
            "start": start,
            "end": end,
            "duration": max(0.0, end - start),
            "headline": headline,
            "caption": short_text,
            "background": palette[(len(raw_scenes) + 1) % len(palette)],
            "tone": "narrative" if len(raw_scenes) % 2 == 0 else "motivation",
            "media_url": "",
            "transition": "fade" if len(raw_scenes) % 2 == 0 else "slide_left",
            "effect": "cinematic" if len(raw_scenes) % 2 == 0 else "glow",
            "caption_style": "bottom" if len(raw_scenes) % 2 == 0 else "center",
        })

    # Snap scene boundaries to beats BEFORE applying overlap
    for scene in raw_scenes:
        source_start = scene["start"]
        source_end = scene["end"]

        snapped_start = force_snap_to_beat(source_start, beats)
        snapped_end = force_snap_to_beat(source_end, beats)

        if snapped_end - snapped_start < MIN_SCENE_DURATION:
            # Extend to next beat if too short
            next_beat = min((b for b in beats if b > snapped_start), default=snapped_start + MIN_SCENE_DURATION)
            snapped_end = max(snapped_end, next_beat)

        scene["source_start"] = round(snapped_start, 3)
        scene["source_end"] = round(snapped_end, 3)
        scene["start"] = snapped_start
        scene["end"] = snapped_end

    # Ensure first scene is a strong hook (<= VIRAL_HOOK_SECONDS)
    if raw_scenes and raw_scenes[0]["end"] - raw_scenes[0]["start"] > VIRAL_HOOK_SECONDS:
        raw_scenes[0]["end"] = raw_scenes[0]["start"] + VIRAL_HOOK_SECONDS
        if beats:
            raw_scenes[0]["end"] = force_snap_to_beat(raw_scenes[0]["end"], beats)
        raw_scenes[0]["source_end"] = raw_scenes[0]["end"]

    # Apply overlap normalization for cross-transitions
    return normalize_scene_timings(raw_scenes)


def make_seamless_loop(
    storyboard: List[Dict[str, Any]],
    loop_duration: float = LOOP_DURATION,
) -> List[Dict[str, Any]]:
    """Ensure last scene's tail matches first scene's head for seamless looping."""
    if not storyboard:
        return storyboard

    first = storyboard[0]
    last = storyboard[-1]

    # Store loop metadata for render engine
    first["loop_head"] = {
        "visual": first.get("media_url", ""),
        "caption": first.get("caption", ""),
        "duration": loop_duration,
    }
    last["loop_tail"] = {
        "visual": last.get("media_url", ""),
        "caption": last.get("caption", ""),
        "duration": loop_duration,
    }

    return storyboard


def generate_optimized_thumbnail(
    storyboard: List[Dict[str, Any]],
    project_name: str,
    width: int = 1080,
    height: int = 1920,
) -> str:
    """Generate a high-CTR thumbnail: first frame with large hook text, high contrast."""
    if not storyboard:
        return ""

    first_scene = storyboard[0]
    media_url = first_scene.get("media_url", "")
    hook_text = first_scene.get("caption", first_scene.get("headline", "Dark Video Studio"))[:60]

    # Create HTML for thumbnail (rendered as single frame)
    thumb_html = f"""<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8" />
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
html, body {{ width:100%; height:100%; overflow:hidden; background:#05070b; }}
#root {{ position:relative; width:100%; height:100%; }}
.bg {{ position:absolute; inset:0; object-fit:cover; filter: brightness(0.55) saturate(1.15); }}
.overlay {{ position:absolute; inset:0; background:linear-gradient(180deg, rgba(5,7,11,0.7) 0%, rgba(5,7,11,0.3) 50%, rgba(5,7,11,0.8) 100%); }}
.text {{ position:absolute; top:50%; left:50%; transform:translate(-50%,-50%);
    font-size:96px; font-weight:900; letter-spacing:-0.02em; text-transform:uppercase;
    color:#fff; text-shadow:0 8px 32px rgba(0,0,0,0.9);
    text-align:center; max-width:90%; line-height:1.05;
    background:rgba(5,7,11,0.65); padding:32px 48px; border-radius:20px; }}
.accent {{ color:#22d3ee; }}
</style>
</head>
<body>
<div id="root" style="width:{width}px;height:{height}px;">
    {'<img class="bg" src="' + media_url + '" alt="">' if media_url else ''}
    <div class="overlay"></div>
    <div class="text">{hook_text.upper()}</div>
</div>
</body>
</html>"""

    thumb_path = THUMB_DIR / f"{project_name}_thumb.html"
    thumb_path.write_text(thumb_html, encoding="utf-8")
    return str(thumb_path)


def build_platform_metadata(
    storyboard: List[Dict[str, Any]],
    topic: str = "",
    niche: str = "",
) -> Dict[str, Any]:
    """Generate TikTok/Reels/Shorts metadata: hashtags, category, sound suggestions."""
    # Extract key terms from captions for hashtags
    all_text = " ".join(scene.get("caption", "") for scene in storyboard)
    words = re.findall(r'\b[a-zA-ZÀ-ÿ]{4,}\b', all_text.lower())
    freq = {}
    for w in words:
        freq[w] = freq.get(w, 0) + 1
    top_terms = sorted(freq, key=freq.get, reverse=True)[:8]

    hashtags = [f"#{term}" for term in top_terms[:5]]
    hashtags += ["#viral", "#fyp", "#shorts", "#reels", "#tiktok"]

    # Category mapping
    category_map = {
        "finance": "Education",
        "money": "Education",
        "business": "Business",
        "tech": "Science & Technology",
        "ai": "Science & Technology",
        "health": "Health",
        "fitness": "Health",
        "lifestyle": "Lifestyle",
        "motivation": "Education",
        "education": "Education",
        "story": "Entertainment",
        "history": "Education",
        "science": "Science & Technology",
    }
    category = "Education"
    for key, cat in category_map.items():
        if key in topic.lower() or key in niche.lower():
            category = cat
            break

    return {
        "hashtags": hashtags,
        "category": category,
        "title_options": [
            storyboard[0].get("caption", "")[:100] if storyboard else topic,
            f"Você não vai acreditar no que descobri sobre {topic or niche}",
            f"A verdade sobre {topic or niche} que ninguém te conta",
        ],
        "description": f"Descubra {topic or niche} em 60 segundos. " + " ".join(hashtags),
        "sound_suggestions": [
            "Trending beat sync",
            "Cinematic tension",
            "Minimal ambient",
        ],
        "posting_times": ["06:00-09:00", "12:00-15:00", "18:00-21:00"],
    }


async def render_viral_video(
    project_name: str,
    srt_path: Path,
    aspect_ratio: str = "vertical",
    include_karaoke: bool = True,
    topic: str = "",
    niche: str = "",
) -> Dict[str, Any]:
    """Full viral pipeline: beat-sync → loop → thumbnail → metadata → render."""
    safe_name = project_name.strip() or "viral_project"

    # Load audio and detect beats
    audio_path = next(
        (item for item in sorted(UPLOAD_DIR.glob(f"{safe_name}.*")) if item.suffix.lower() in AUDIO_SUFFIXES),
        None,
    )
    beat_data = {"tempo": 120.0, "beats": [], "beat_count": 0, "duration": 0.0}
    if audio_path and audio_path.exists():
        beat_data = detect_beats(audio_path)

    # Parse transcript and build beat-synced storyboard
    segments = parse_srt_to_segments(srt_path) or []
    storyboard = build_beat_synced_storyboard(segments, beat_data.get("beats", []), aspect_ratio)

    # Seamless loop
    storyboard = make_seamless_loop(storyboard)

    # Search media per scene
    scene_media, term_source = search_media_for_scenes(storyboard, niche=niche.strip() or topic.strip())
    global_media = search_media_for_keywords(extract_keywords_from_text(srt_path.read_text(encoding="utf-8")))
    media_pool = global_media or [item for items in scene_media.values() for item in items]

    # Assign media and viral settings to storyboard
    for idx, scene in enumerate(storyboard):
        candidates = scene_media.get(idx) or []
        media_item = candidates[0] if candidates else {}
        scene["media_url"] = scene.get("media_url") or media_item.get("url", "")
        scene["search_terms"] = [item.get("keyword", "") for item in candidates[:3]]
        scene["transition"] = "wipe"
        scene["effect"] = "glow" if idx == 0 else "cinematic"
        scene["caption_style"] = "karaoke" if include_karaoke and idx == 0 else "bottom"
        scene["provider"] = media_item.get("source", "none")
        scene["beat_synced"] = True

    # Build edit plan
    edit_plan = build_edit_plan_from_segments(segments, media_pool)
    for idx, scene in enumerate(edit_plan):
        candidates = scene_media.get(idx) or []
        scene["transition"] = "wipe"
        scene["effect"] = "glow" if idx == 0 else "cinematic"
        scene["captions"] = True
        scene["search_terms"] = [item.get("keyword", "") for item in candidates[:3]]
        if candidates:
            scene["media_url"] = candidates[0].get("url", "")

    # Generate thumbnail
    width, height = get_dimensions(aspect_ratio)
    thumbnail_path = generate_optimized_thumbnail(storyboard, safe_name, width, height)

    # Platform metadata
    platform_meta = build_platform_metadata(storyboard, topic, niche)

    # Render
    pool_urls = [item.get("url", "") for item in media_pool if item.get("url")]
    render_result = await render_video_hyperframes(
        safe_name, srt_path, storyboard, audio_path, aspect_ratio,
        media_pool=pool_urls,
        output_stem=f"{safe_name}_viral",
    )

    # Save metadata
    meta = {
        "project_name": safe_name,
        "beat_data": beat_data,
        "platform_metadata": platform_meta,
        "thumbnail_path": thumbnail_path,
        "storyboard": storyboard,
        "edit_plan": edit_plan,
        "render": render_result,
        "loop_duration": LOOP_DURATION,
        "hook_duration": storyboard[0]["duration"] if storyboard else 0,
    }
    meta_path = OUTPUT_DIR / f"{safe_name}_viral_meta.json"
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    render_result["viral_meta"] = meta
    return render_result