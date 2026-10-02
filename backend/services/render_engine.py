from __future__ import annotations

from pathlib import Path
from typing import List, Dict, Any, Optional
import json
import subprocess
import asyncio
import os
import tempfile
import shutil

from backend.services.pipeline import (
    BASE_DIR,
    OUTPUT_DIR,
    UPLOAD_DIR,
    STORAGE_DIR,
    get_media_duration,
    download_media_asset,
    fit_storyboard_to_duration,
    build_storyboard_from_segments,
    parse_srt_to_segments,
)
from backend.services.asset_quality import looks_padded

import sys
HYPERFRAMES_CLI = "npx.cmd" if sys.platform == "win32" else "npx"
HYPERFRAMES_VERSION = "0.8.92"

# CSS template loaded at module level
_TRANSITIONS_CSS = """
        .clip {
            position: absolute;
            inset: 0;
            overflow: hidden;
        }
        .scene {
            display: block;
        }
        .scene-media {
            position: absolute;
            inset: 0;
            width: 100%;
            height: 100%;
            object-fit: cover;
            display: block;
        }
        .scene-tint {
            position: absolute;
            inset: 0;
            background: linear-gradient(
                180deg,
                rgba(0, 0, 0, 0) 0%,
                rgba(0, 0, 0, 0) 58%,
                rgba(0, 0, 0, 0.42) 82%,
                rgba(0, 0, 0, 0.58) 100%
            );
            pointer-events: none;
        }
        .caption {
            display: flex;
            align-items: flex-end;
            justify-content: center;
            text-align: center;
            padding: 0 8% 11%;
            pointer-events: none;
        }
        .caption.center { align-items: center; padding-bottom: 0; }
        .caption.bottom { align-items: flex-end; }
        .caption.hook { align-items: center; padding-bottom: 4%; }
        .caption-text {
            font-size: 52px;
            font-weight: 800;
            line-height: 1.18;
            color: #ffffff;
            text-shadow: 0 4px 14px rgba(0,0,0,0.85);
            max-width: 100%;
        }
        .caption.hook .caption-text {
            font-size: 76px;
            font-weight: 900;
            letter-spacing: -0.02em;
            text-transform: uppercase;
            /* The hook sits mid-frame where the tint is weak, and a bright photo
               would wash it out; a plate guarantees contrast on any footage. */
            background: rgba(5, 7, 11, 0.68);
            padding: 22px 38px;
            border-radius: 16px;
            display: block;
        }
        .caption.karaoke .caption-text {
            background: rgba(0,0,0,0.62);
            padding: 18px 34px;
            border-radius: 14px;
            display: block;
        }
        .caption.karaoke .karaoke-word {
            display: inline-block;
            opacity: 0.32;
            transition: none;
        }
        #grain-overlay {
            position: absolute;
            inset: 0;
            pointer-events: none;
            z-index: 60;
        }
        #grain-overlay .grain-texture {
            position: absolute;
            inset: 0;
            /* Pure-CSS texture: an SVG data-URI trips the renderer's CSS parser
               ("Unclosed string" on the encoded quotes), and a dot pattern reads
               as grain at video resolution without a 330-char URL. */
            background-image:
                radial-gradient(rgba(255, 255, 255, 0.055) 1px, transparent 1.2px),
                radial-gradient(rgba(255, 255, 255, 0.035) 1px, transparent 1.4px);
            background-size: 4px 4px, 7px 7px;
            background-position: 0 0, 2px 3px;
            opacity: 0.9;
        }
        #progress-track {
            position: absolute;
            left: 4%;
            right: 4%;
            bottom: 3.2%;
            height: 5px;
            border-radius: 3px;
            /* 3.2:1 minimum against the dark root; 0.22 alpha measures ~3.6:1. */
            background: rgba(255, 255, 255, 0.22);
            pointer-events: none;
            z-index: 55;
            overflow: hidden;
        }
        #progress-track .progress-fill {
            position: absolute;
            inset: 0;
            background: linear-gradient(90deg, #22d3ee, #67e8f9);
            border-radius: 3px;
            transform-origin: left center;
        }
        """


def get_dimensions(aspect_ratio: str) -> tuple[int, int]:
    dimensions = {
        "vertical": (1080, 1920),
        "square": (1080, 1080),
        "landscape": (1920, 1080),
    }
    return dimensions.get(aspect_ratio, dimensions["vertical"])


def escape_html(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#x27;")
    )


def _grain_overlay(total_duration: float) -> tuple[str, List[str]]:
    """Film grain overlay plus its deterministic step animations.

    The grain texture steps through discrete background positions at absolute
    times. A CSS ``infinite`` animation would advance on wall-clock time and make
    every render slightly different; bounded absolute steps are seek-safe.
    """
    markup = '<div id="grain-overlay"><div class="grain-texture"></div></div>'
    animations: List[str] = []
    steps = max(1, int(round(total_duration / _GRAIN_STEP)))
    for i in range(steps):
        position = f"{(i % _GRAIN_POSITIONS) * -7}% {(i % _GRAIN_POSITIONS) * 5}%"
        animations.append(
            f'tl.set("#grain-overlay .grain-texture", '
            f'{{ backgroundPosition: "{position}" }}, {round(i * _GRAIN_STEP, 3)});'
        )
    return markup, animations


def _progress_bar(total_duration: float) -> tuple[str, List[str]]:
    """A thin elapsed-time bar. scaleX is tweened across the whole duration."""
    markup = '<div id="progress-track"><div class="progress-fill"></div></div>'
    animation = (
        f'tl.fromTo("#progress-track .progress-fill", {{ scaleX: 0 }}, '
        f'{{ scaleX: 1, duration: {round(total_duration, 3)}, ease: "none", immediateRender: false }}, 0);'
    )
    return markup, [animation]


def _scene_media_animations(idx, start, duration, effect, has_next, next_start) -> List[str]:
    """Per-scene media motion: fade in, Ken Burns, then the cross-transition.

    Scene ``idx`` is the incoming side of the transition from the previous scene,
    so its wipe runs across the first ``_WIPE`` seconds of its own window — that is
    exactly when the previous scene is still visible behind it. Its own drift-back
    and blur run when the *next* scene wipes in, at ``next_start``.

    Opacity, scale and filter are tweened separately so the tweens never fight; the
    base grade rides inside every filter value, and the Ken Burns ends where the
    shrink begins so the two scale tweens stay sequential.
    """
    media_id = f"#{escape_html(f'scene-media-{idx}')}"
    animations: List[str] = []

    fade_duration = 0.35 if effect == "clean" else 0.8
    animations.append(
        f'tl.fromTo("{media_id}", {{ opacity: 0 }}, '
        f'{{ opacity: 1, duration: {fade_duration}, ease: "power2.out", immediateRender: false }}, {start});'
    )

    # Wipe in over the previous scene: nothing before the first scene to reveal over.
    has_wipe_in = idx > 0 and duration >= 2.0
    if has_wipe_in:
        animations.append(
            f'tl.fromTo("{media_id}", {{ clipPath: "inset(0% 100% 0% 0%)" }}, '
            f'{{ clipPath: "inset(0% 0% 0% 0%)", duration: {_WIPE}, ease: "power2.inOut", immediateRender: false }}, '
            f'{round(start, 3)});'
        )

    # Ken Burns across the scene, ending where the shrink will begin.
    ken_from, ken_to = (1.08, 1.0) if effect == "glow" else (1.0, 1.08)
    ken_end = round(next_start, 3) if has_next else round(start + duration, 3)
    ken_duration = max(0.2, ken_end - start)
    animations.append(
        f'tl.fromTo("{media_id}", {{ scale: {ken_from} }}, '
        f'{{ scale: {ken_to}, duration: {round(ken_duration, 3)}, ease: "none", immediateRender: false }}, {start});'
    )

    # Outgoing side: drift back and blur while the next scene wipes over this one.
    if has_next:
        # overwrite:"auto" because the shrink starts exactly where the Ken Burns
        # ends; the lint reads that touching point as an overlap.
        animations.append(
            f'tl.fromTo("{media_id}", {{ scale: {ken_to} }}, '
            f'{{ scale: 0.97, duration: {_WIPE}, ease: "power2.inOut", immediateRender: false, overwrite: "auto" }}, '
            f'{round(next_start, 3)});'
        )
        animations.append(
            f'tl.fromTo("{media_id}", {{ filter: "{_GRADE} blur(0px)" }}, '
            f'{{ filter: "{_GRADE} blur(5px)", duration: {_WIPE}, ease: "power2.inOut", immediateRender: false, overwrite: "auto" }}, '
            f'{round(next_start, 3)});'
        )

    return animations

# Base grade baked into every filter tween. Living in the tweens rather than a CSS
# filter keeps GSAP from fighting a stylesheet value on the same property.
_GRADE = "brightness(1.06) saturate(1.08)"
# Cross-transition window: how long the incoming wipe takes and how long the
# outgoing scene drifts back under it.
_WIPE = 0.7
# Grain steps through discrete positions on absolute times: deterministic at any
# frame, unlike a CSS infinite animation, which runs on wall-clock time.
_GRAIN_STEP = 0.5
_GRAIN_POSITIONS = 8

VIDEO_EXTENSIONS = {".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm", ".m4v": "video/mp4"}
IMAGE_EXTENSIONS = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp", ".bmp": "image/bmp"}


def stage_project_assets(
    project_name: str,
    storyboard: List[Dict[str, Any]],
    audio_path: Optional[Path],
    assets_dir: Path,
    media_pool: Optional[List[str]] = None,
) -> tuple[Dict[int, str], Optional[str], List[Dict[str, Any]]]:
    """Copy every referenced asset into the composition's own ``assets/`` directory.

    HyperFrames serves the composition from its project directory and the renderer
    refuses local resources outside it, so absolute paths into ``storage/media``
    fail to load. Returns ``{scene_index: relative_ref}``, the audio ref, and a
    report of any asset swapped out for a better candidate.

    Some stock providers return images wrapped in a large uniform white canvas.
    ``object-fit: cover`` renders that canvas faithfully, so such an asset looks
    like a broken video; those are rejected in favour of the next candidate.
    """
    assets_dir.mkdir(parents=True, exist_ok=True)

    media_refs: Dict[int, str] = {}
    rejected: List[Dict[str, Any]] = []

    for idx, scene in enumerate(storyboard):
        scene_url = str(scene.get("media_url") or "").strip()
        if not scene_url:
            continue

        candidates = [scene_url]
        for extra in media_pool or []:
            if extra and extra not in candidates:
                candidates.append(extra)

        chosen: Optional[str] = None
        for position, candidate in enumerate(candidates):
            asset_path = download_media_asset(candidate, project_name, idx)
            if not asset_path or not asset_path.exists():
                continue
            suffix = asset_path.suffix.lower()
            if suffix not in IMAGE_EXTENSIONS and suffix not in VIDEO_EXTENSIONS:
                continue
            if suffix in IMAGE_EXTENSIONS and looks_padded(asset_path):
                rejected.append({
                    "scene": idx,
                    "rejected_url": candidate,
                    "reason": "padded canvas",
                })
                continue
            chosen = candidate
            try:
                shutil.copyfile(asset_path, assets_dir / asset_path.name)
            except OSError:
                chosen = None
                continue
            media_refs[idx] = f"assets/{asset_path.name}"
            scene["media_url"] = candidate
            scene["media_substituted"] = position > 0
            break

    audio_ref: Optional[str] = None
    if audio_path and audio_path.exists():
        try:
            shutil.copyfile(audio_path, assets_dir / audio_path.name)
            audio_ref = f"assets/{audio_path.name}"
        except OSError:
            audio_ref = None

    return media_refs, audio_ref, rejected


def _scene_times(scene: Dict[str, Any], idx: int) -> tuple[float, float]:
    start = max(0.0, float(scene.get("start", idx * 3.0)))
    duration = max(0.2, float(scene.get("duration", 3.0)))
    return round(start, 3), round(duration, 3)


def _build_scene_media(ref: Optional[str], idx: int) -> str:
    """Build the inner media node for a scene from a project-relative asset ref."""
    if not ref:
        return ""
    suffix = Path(ref).suffix.lower()
    media_id = escape_html(f"scene-media-{idx}")
    safe_ref = escape_html(ref)

    if suffix in VIDEO_EXTENSIONS:
        # No data-start on the <video>: timing lives on the wrapping clip only.
        return (
            f'<video id="{media_id}" class="scene-media" muted playsinline '
            f'preload="auto" src="{safe_ref}"></video>'
        )
    if suffix in IMAGE_EXTENSIONS:
        return f'<img id="{media_id}" class="scene-media" src="{safe_ref}" alt="" />'
    return ""


def generate_composition_html(
    project_name: str,
    storyboard: List[Dict[str, Any]],
    media_refs: Dict[int, str],
    audio_ref: Optional[str],
    aspect_ratio: str,
    total_duration: float,
) -> str:
    width, height = get_dimensions(aspect_ratio)

    scene_clips: List[str] = []
    caption_clips: List[str] = []
    animations: List[str] = []

    for idx, scene in enumerate(storyboard):
        start, duration = _scene_times(scene, idx)
        scene_id = escape_html(f"scene-{idx}")
        background = str(scene.get("background", "#0f172a") or "#0f172a")
        effect = str(scene.get("effect", "cinematic") or "cinematic").lower()
        media_html = _build_scene_media(media_refs.get(idx), idx)

        inner = media_html or ""
        if media_html:
            inner += '<div class="scene-tint"></div>'

        scene_clips.append(
            f'<div id="{scene_id}" class="clip scene" data-start="{start}" '
            f'data-duration="{duration}" data-track-index="0" data-layout-allow-overflow '
            f'style="background:{escape_html(background)};">{inner}</div>'
        )

        has_next = idx + 1 < len(storyboard)
        next_start = float(storyboard[idx + 1]["start"]) if has_next else 0.0
        animations.extend(
            _scene_media_animations(idx, start, duration, effect, has_next, next_start)
        )

        caption = str(scene.get("caption") or scene.get("headline") or "").strip()
        caption_style = str(scene.get("caption_style", "bottom") or "bottom")
        words = scene.get("words") or []
        if idx == 0 and caption_style == "bottom":
            # The opening frame is the hook: bigger, centred, uppercase.
            caption_style = "hook"
        if caption and caption_style != "hidden":
            if caption_style == "karaoke" and words:
                # Join with a space: inline-block spans otherwise run together.
                inner_caption = " ".join(
                    f'<span class="karaoke-word">{escape_html(str(word.get("word", "")))}</span>'
                    for word in words
                )
            else:
                # Plain text: the outer span below is the single wrapper.
                inner_caption = escape_html(caption)

            # Captions never overlap: this one ends when the next caption appears.
            caption_start = round(start + 0.15, 3)
            caption_end = round(min(start + duration, next_start + 0.15), 3) if has_next else round(start + duration, 3)
            caption_duration = max(0.3, caption_end - caption_start)

            caption_clips.append(
                f'<div id="caption-{idx}" class="clip caption {escape_html(caption_style)}" '
                f'data-start="{caption_start}" data-duration="{caption_duration}" data-track-index="1">'
                f'<span class="caption-text">{inner_caption}</span></div>'
            )
            caption_id = f"#caption-{idx} .caption-text"
            animations.append(
                f'tl.fromTo("{caption_id}", {{ opacity: 0, y: 26 }}, '
                f'{{ opacity: 1, y: 0, duration: 0.45, ease: "power2.out", immediateRender: false }}, '
                f'{caption_start});'
            )
            # Karaoke: light each word at its own timestamp inside this scene.
            if caption_style == "karaoke" and words:
                for word_idx, word in enumerate(words):
                    word_start = float(word.get("start", start))
                    word_dur = max(0.08, float(word.get("end", word_start)) - word_start)
                    selector = f"#caption-{idx} .karaoke-word:nth-of-type({word_idx + 1})"
                    animations.append(
                        f'tl.fromTo("{selector}", {{ opacity: 0.32, scale: 0.97 }}, '
                        f'{{ opacity: 1, scale: 1.02, duration: {round(word_dur, 3)}, ease: "none", immediateRender: false }}, '
                        f'{round(max(word_start, start), 3)});'
                    )

    audio_element = ""
    if audio_ref:
        audio_element = (
            f'<audio id="main-audio" src="{escape_html(audio_ref)}" '
            f'data-start="0" data-duration="{round(total_duration, 3)}" preload="auto"></audio>'
        )

    grain_markup, grain_animations = _grain_overlay(total_duration)
    progress_markup, progress_animations = _progress_bar(total_duration)

    tracks_html = "".join(scene_clips) + "".join(caption_clips)

    # The CSS block is interpolated raw: escaping it would corrupt the grain
    # texture, and Python comments must stay outside the template because an
    # f-string emits everything literally.
    html = f"""<!doctype html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width={width}, height={height}" />
    <script src="https://cdn.jsdelivr.net/npm/gsap@3.14.2/dist/gsap.min.js"></script>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        html, body {{ width: 100%; height: 100%; overflow: hidden; background: #05070b; }}
        body {{ font-family: 'Segoe UI', Tahoma, Arial, sans-serif; }}
        #root {{ position: relative; width: 100%; height: 100%; overflow: hidden; background: #05070b; }}
        {_TRANSITIONS_CSS}
    </style>
</head>
<body>
    <div id="root" data-composition-id="main" data-start="0" data-duration="{round(total_duration, 3)}"
         data-width="{width}" data-height="{height}">
        {tracks_html}
        {grain_markup}
        {progress_markup}
    </div>
    {audio_element}
    <script>
        const tl = gsap.timeline({{ paused: true }});
        {"".join(animations)}
        {"".join(grain_animations)}
        {"".join(progress_animations)}
        window.__timelines["main"] = tl;
    </script>
</body>
</html>"""
    return html


def create_project_dir(project_name: str) -> Path:
    """Create (or reset) the HyperFrames project directory for a render.

    Lives under OUTPUT_DIR rather than a temp dir so Chrome/Puppeteer never races
    a cleanup, and is wiped first so assets from a previous run never leak in.
    """
    project_dir = OUTPUT_DIR / f".hf_{project_name}"
    if project_dir.exists():
        shutil.rmtree(project_dir, ignore_errors=True)
    project_dir.mkdir(parents=True, exist_ok=True)

    hyperframes_json = {
        "$schema": "https://hyperframes.heygen.com/schema/hyperframes.json",
        "registry": "https://raw.githubusercontent.com/heygen-com/hyperframes/main/registry",
        "paths": {
            "blocks": "compositions",
            "components": "compositions/components",
            "assets": "assets",
        },
        "media": {"autoProxy": True},
    }
    (project_dir / "hyperframes.json").write_text(json.dumps(hyperframes_json, indent=2), encoding="utf-8")

    package_json = {
        "name": project_name,
        "private": True,
        "type": "module",
        "scripts": {
            "render": f"npx --yes hyperframes@{HYPERFRAMES_VERSION} render",
            "check": f"npx --yes hyperframes@{HYPERFRAMES_VERSION} check",
        },
    }
    (project_dir / "package.json").write_text(json.dumps(package_json, indent=2), encoding="utf-8")

    return project_dir


def cleanup_render_dirs(keep: Optional[Path] = None) -> int:
    """Remove leftover HyperFrames working directories.

    Each render creates ``OUTPUT_DIR/.hf_<name>``. They used to accumulate
    indefinitely (11 had piled up), so clear them after a successful render while
    keeping the one just used, which is useful for inspecting the composition.
    """
    removed = 0
    keep_resolved = keep.resolve() if keep else None
    for item in OUTPUT_DIR.glob(".hf_*"):
        if not item.is_dir():
            continue
        if keep_resolved is not None and item.resolve() == keep_resolved:
            continue
        shutil.rmtree(item, ignore_errors=True)
        removed += 1
    return removed


async def render_with_hyperframes(
    project_name: str,
    composition_html: str,
    output_path: Path,
    project_dir: Path,
    width: int,
    height: int,
) -> Dict[str, Any]:
    (project_dir / "index.html").write_text(composition_html, encoding="utf-8")

    try:
        # exec with an argument list, not a shell string: no quoting surprises.
        proc = await asyncio.create_subprocess_exec(
            HYPERFRAMES_CLI,
            "--yes",
            f"hyperframes@{HYPERFRAMES_VERSION}",
            "render",
            "-o",
            str(output_path),
            cwd=str(project_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            raise RuntimeError(f"HyperFrames render failed: {stderr.decode(errors='replace')}")

        if not output_path.exists():
            raise RuntimeError("No MP4 output found after render")

        return {
            "status": "rendered",
            "output_path": str(output_path),
            "stdout": stdout.decode(errors="replace"),
        }
    except FileNotFoundError as exc:
        raise RuntimeError("npx not found. Please install Node.js and npm.") from exc


async def render_video_hyperframes(
    project_name: str,
    srt_path: Path,
    storyboard: List[Dict[str, Any]] | None = None,
    audio_path: Path | None = None,
    aspect_ratio: str = "vertical",
    output_stem: str | None = None,
    media_pool: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Render a project to MP4.

    ``output_stem`` keeps derived renders (e.g. the shorts cut) from overwriting the
    main video for the same project.
    """
    stem = output_stem or project_name
    output_path = OUTPUT_DIR / f"{stem}.mp4"
    audio_duration = get_media_duration(audio_path) if audio_path and audio_path.exists() else None
    
    if storyboard is None:
        try:
            segments = parse_srt_to_segments(srt_path)
            storyboard = build_storyboard_from_segments(segments[:6])
        except Exception:
            storyboard = [{
                "index": 1, "start": 0.0, "end": 6.0, "duration": 6.0,
                "headline": "Dark Video Studio", "caption": "Dark Video Studio MVP",
                "background": "#0f172a", "tone": "narrative", "media_url": "",
            }]
    
    if not storyboard:
        storyboard = [{
            "index": 1, "start": 0.0, "end": 6.0, "duration": 6.0,
            "headline": "Dark Video Studio", "caption": "Dark Video Studio MVP",
            "background": "#0f172a", "tone": "narrative", "media_url": "",
        }]
    
    if audio_duration:
        storyboard = fit_storyboard_to_duration(storyboard, audio_duration)
    
    total_duration = audio_duration or max(s.get("end", 0) for s in storyboard)

    # Stage assets into the composition's own directory, then reference them relatively.
    project_dir = create_project_dir(stem)
    media_refs, audio_ref, rejected_assets = stage_project_assets(
        project_name, storyboard, audio_path, project_dir / "assets", media_pool
    )

    composition_html = generate_composition_html(
        stem, storyboard, media_refs, audio_ref, aspect_ratio, total_duration
    )

    width, height = get_dimensions(aspect_ratio)

    try:
        result = await render_with_hyperframes(
            stem, composition_html, output_path, project_dir, width, height
        )
        cleanup_render_dirs(keep=project_dir)

        video_duration = get_media_duration(output_path)

        payload = {
            "project_name": project_name,
            "output_stem": stem,
            "status": "rendered",
            "srt_path": str(srt_path),
            "output_path": str(output_path),
            "storyboard": storyboard,
            "scene_count": len(storyboard),
            "scenes_with_media": len(media_refs),
            "rejected_assets": rejected_assets,
            "aspect_ratio": aspect_ratio,
            "resolution": f"{width}x{height}",
            "audio_duration": audio_duration,
            "video_duration": video_duration,
            "message": "Vídeo renderizado com HyperFrames com sucesso.",
            "render_engine": "hyperframes",
        }
    except Exception as e:
        payload = {
            "project_name": project_name,
            "output_stem": stem,
            "status": "error",
            "error": str(e),
            "message": f"Falha no render HyperFrames: {e}",
        }

    summary_file = OUTPUT_DIR / f"{stem}.json"
    summary_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


async def preview_with_hyperframes(
    project_name: str,
    composition_html: str,
    port: int = 8081,
) -> Dict[str, Any]:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        project_dir = tmpdir_path / project_name
        project_dir.mkdir(parents=True)
        
        (project_dir / "index.html").write_text(composition_html, encoding="utf-8")
        
        hyperframes_json = {
            "$schema": "https://hyperframes.heygen.com/schema/hyperframes.json",
            "registry": "https://raw.githubusercontent.com/heygen-com/hyperframes/main/registry",
            "paths": {"blocks": "compositions", "components": "compositions/components", "assets": "assets"},
            "media": {"autoProxy": True}
        }
        (project_dir / "hyperframes.json").write_text(json.dumps(hyperframes_json, indent=2), encoding="utf-8")
        
        package_json = {
            "name": project_name, "private": True, "type": "module",
            "scripts": {"dev": f"npx --yes hyperframes@{HYPERFRAMES_VERSION} preview --background --port {port}"}
        }
        (project_dir / "package.json").write_text(json.dumps(package_json, indent=2), encoding="utf-8")
        
        proc = await asyncio.create_subprocess_exec(
            HYPERFRAMES_CLI, "--yes", f"hyperframes@{HYPERFRAMES_VERSION}", "preview", "--background", f"--port={port}",
            cwd=str(project_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        
        await asyncio.sleep(3)
        
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5)
        except asyncio.TimeoutError:
            pass
        
        return {
            "preview_url": f"http://localhost:{port}",
            "project_dir": str(project_dir),
            "process": proc,
        }