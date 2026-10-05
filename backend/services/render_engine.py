"""HyperFrames rendering: composition HTML, the render subprocess, the music mix.

One setting reaches every machine: ``DARK_STUDIO_RENDER_TIMEOUT`` (seconds,
default 900) bounds how long a single ``npx hyperframes render`` may run before
its whole process tree is killed. It is read from the environment on every call
(:func:`render_timeout_seconds`), never at import time, so a test or an operator
can override it without restarting anything.

The default is deliberately generous - 900s, not 120s - because a real render
legitimately runs for minutes: ~316s measured for a 41s 1080x1920 cut (1237
frames at ~3.9 fps) on a 2-vCPU box, and slower hardware scales from there. The
watchdog exists to catch a wedged chrome/ffmpeg and to let the server shut down,
not to police render speed, so it must never cut a healthy slow render short.
"""
from __future__ import annotations

from pathlib import Path
from typing import List, Dict, Any, Optional
import json
import subprocess
# Bound directly: the mixer catches these by type, and a test may swap the
# subprocess module out from under us to simulate a missing ffmpeg.
from subprocess import SubprocessError
import asyncio
import os
import signal
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
from backend.services import music
from backend.services.style import (
    VALID_CAPTION_MODES,
    SubtitleStyle,
    align_for_position,
    get_preset,
    resolve_preset_and_style,
    valid_choice,
)

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


# The static variant rules below (.caption.hook .caption-text and friends) are
# two classes deep, so a bare ".caption-text" rule can never outrank them no
# matter where it lands in the sheet. A configured style therefore repeats that
# exact specificity for every caption variant - same selector shape, later in
# the document - which lets the user's typography win without !important.
# "hidden" is absent because that variant emits no caption element at all.
_CAPTION_VARIANTS: tuple[str, ...] = ("bottom", "center", "hook", "karaoke")
_CAPTION_CONTAINER_SELECTORS = ", ".join(f".caption.{name}" for name in _CAPTION_VARIANTS)
_CAPTION_TEXT_SELECTORS = ", ".join(
    f".caption.{name} .caption-text" for name in _CAPTION_VARIANTS
)


def build_subtitle_css(style: SubtitleStyle) -> str:
    """The CSS a configured :class:`SubtitleStyle` contributes to the composition.

    Two rules, both interpolated raw into the ``<style>`` block and both safe by
    construction: ``style.to_css()`` returns bare allowlisted tokens (no braces,
    semicolons, quotes, newlines or ``url()`` payloads), and the container rule
    carries only an ``align-items`` keyword from ``align_for_position``.

    The block is emitted *after* ``_TRANSITIONS_CSS`` and mirrors the variant
    selectors' specificity, so a configured size/colour/stroke actually wins
    over the hardcoded hook and karaoke variants.
    """
    declarations = "; ".join(
        f"{prop}: {value}" for prop, value in style.to_css().items()
    )
    align = align_for_position(style.position)
    return (
        f"{_CAPTION_CONTAINER_SELECTORS} {{ align-items: {align}; }}\n"
        f"{_CAPTION_TEXT_SELECTORS} {{ {declarations}; }}"
    )


def apply_preset_to_storyboard(
    storyboard: List[Dict[str, Any]], preset: Optional[str]
) -> List[Dict[str, Any]]:
    """Return copies of *storyboard* restyled by *preset*; the input is untouched.

    A scene's own ``background`` is an explicit authorial choice and always
    wins; only scenes that carry none get a colour from the preset palette,
    cycled by scene index so consecutive frames never repeat. ``transition`` and
    ``effect`` are preset-owned by definition, so they are set unconditionally -
    a preset is a look, not a suggestion.

    With no preset name the scenes come back as plain copies, which is what
    keeps an unconfigured render byte-identical to the pre-preset output.
    """
    scenes: List[Dict[str, Any]] = [dict(scene) for scene in storyboard or []]
    if not preset or not isinstance(preset, str) or not preset.strip():
        return scenes

    resolved = get_preset(preset)
    palette = resolved.palette or []
    for idx, scene in enumerate(scenes):
        if palette and not str(scene.get("background") or "").strip():
            scene["background"] = palette[idx % len(palette)]
        scene["transition"] = resolved.transition
        scene["effect"] = resolved.effect
    return scenes


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

# ffprobe only has to read a stream list, so it gets a short leash; the ffmpeg
# mix re-reads the whole file and is allowed a long one.
_FFPROBE_TIMEOUT = 60
_MIX_TIMEOUT = 900
# Watchdog for the HyperFrames render itself, in seconds, read from the
# environment on every call so a test or an operator can retune it without
# restarting the server. See render_timeout_seconds() for the reasoning.
_RENDER_TIMEOUT_ENV = "DARK_STUDIO_RENDER_TIMEOUT"
DEFAULT_RENDER_TIMEOUT = 900.0
# How long a terminated render gets to die before it is killed outright, and
# how long taskkill is allowed to take on Windows. Both bound a cleanup path,
# so they must be short.
_KILL_GRACE_SECONDS = 5.0
_TASKKILL_TIMEOUT = 15.0
# 192 kbps AAC is transparent enough for a narration-plus-bed mix and keeps the
# stream well under the 200 kbps where audible artefacts show up.
_MIX_AUDIO_BITRATE = "192k"

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
            if suffix in IMAGE_EXTENSIONS and _is_padded(asset_path):
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


def _is_padded(asset_path: Path) -> bool:
    """Best-effort version of :func:`looks_padded` that cannot kill a render.

    The padding probe shells out to ffprobe/ffmpeg, which the install docs list
    as an *optional* dependency: "sem ffmpeg, ``looks_padded`` devolve sempre
    False". A missing binary made the raw call raise ``FileNotFoundError`` out
    of ``stage_project_assets``, and because staging happens before the render
    try-block in :func:`render_video_hyperframes`, that took the whole request
    down instead of merely skipping the quality check. The probe is advisory:
    when it cannot run, the asset is kept, which is the same outcome as a clean
    image and never the "broken frame" the check exists to prevent.
    """
    try:
        return bool(looks_padded(asset_path))
    except (OSError, ValueError, SubprocessError):
        return False


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
    *,
    preset: Optional[str] = None,
    subtitle_style: Optional[dict] = None,
) -> str:
    """Build the HyperFrames composition for one render.

    ``preset`` and ``subtitle_style`` are optional and keyword-only, so every
    existing caller keeps working untouched. When neither is supplied the output
    is unchanged from before styling existed: no extra CSS is emitted, the
    opening frame keeps its "hook" promotion, and the storyboard is used as-is.

    When either is supplied the look is authored: the preset palette and
    transition are applied to copies of the scenes, the caption typography comes
    from the resolved style, its ``mode`` becomes the default caption variant
    and its ``position`` becomes the container alignment. A scene's own
    ``caption_style`` still wins over that default - that is how the viral
    pipeline puts karaoke on scene 0 and plain captions everywhere else.
    """
    width, height = get_dimensions(aspect_ratio)

    # "Configured" means the caller asked for a look, not merely that a default
    # style exists: resolve_preset_and_style always returns a value, so only the
    # presence of a preset name or a style dict can switch the styled path on.
    styled = bool(preset) or isinstance(subtitle_style, dict)
    resolved_preset, style = resolve_preset_and_style(preset, subtitle_style)
    scenes = apply_preset_to_storyboard(storyboard, preset)
    default_mode = style.mode

    # The styled block is interpolated raw (style.py guarantees every value is
    # inert) and its explanatory comment lives here, in the generated HTML,
    # because a comment inside a value is what trips the renderer's fragile CSS
    # parser. When unstyled this is the empty string, so the surrounding template
    # stays byte-identical to the pre-styling output.
    subtitle_css = ""
    if styled:
        subtitle_css = (
            "\n        /* Configurable caption styling. Every value below is "
            "validated by style.py and safe to interpolate raw; no data-URI may "
            "appear here, because the renderer parser fails on the encoded "
            "quotes it would need. */\n        "
            + build_subtitle_css(style)
        )

    scene_clips: List[str] = []
    caption_clips: List[str] = []
    animations: List[str] = []

    for idx, scene in enumerate(scenes):
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

        has_next = idx + 1 < len(scenes)
        next_start = float(scenes[idx + 1]["start"]) if has_next else 0.0
        animations.extend(
            _scene_media_animations(idx, start, duration, effect, has_next, next_start)
        )

        caption = str(scene.get("caption") or scene.get("headline") or "").strip()
        # A scene's own caption_style is a deliberate per-scene override and wins
        # over the configured style's mode; an unknown value falls back to that
        # mode rather than landing in the markup as an arbitrary class name.
        scene_mode = str(scene.get("caption_style") or "").strip()
        caption_style = (
            valid_choice(scene_mode, VALID_CAPTION_MODES, default_mode)
            if scene_mode
            else default_mode
        )
        if not styled and idx == 0 and caption_style == "bottom":
            # Legacy hook default: with no style configured the opening frame is
            # the hook, so an unconfigured project renders exactly as it always
            # has. Once a look is configured, its mode decides instead.
            caption_style = "hook"
        words = scene.get("words") or []
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
    # f-string emits everything literally. {subtitle_css} is empty unless a
    # look was configured, so an unstyled render is unchanged byte for byte.
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
        {_TRANSITIONS_CSS}{subtitle_css}
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


def _is_windows() -> bool:
    """True on Windows, where process control is done by ``taskkill``, not signals."""
    return sys.platform == "win32"


def render_timeout_seconds() -> float:
    """Seconds one render may run before the watchdog tears its tree down.

    Read from ``DARK_STUDIO_RENDER_TIMEOUT`` on *every* call rather than once at
    import, so a test can shrink the budget and an operator can raise it without
    the value being frozen into the module graph. An unset, blank, unparseable
    or non-positive value falls back to :data:`DEFAULT_RENDER_TIMEOUT`.

    The default has to clear a real render with room to spare, not a plausible
    one: ~316s measured here for a 41s 1080x1920 cut (1237 frames at ~3.9 fps)
    on a 2-vCPU box, and slower hardware or a longer cut scales from there.
    900s is roughly three times that measurement, so the watchdog only ever
    catches a genuinely wedged chrome/ffmpeg - never a valid slow render.
    """
    raw = os.environ.get(_RENDER_TIMEOUT_ENV)
    if raw is None:
        return DEFAULT_RENDER_TIMEOUT
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_RENDER_TIMEOUT
    return value if value > 0 else DEFAULT_RENDER_TIMEOUT


def _discard_partial_output(output_path: Path, existed_before: bool) -> None:
    """Delete the truncated MP4 an abandoned render left behind.

    Only when this render created the file. A previous good render of the same
    project sits at this exact path, and losing a finished video because a later
    attempt timed out would be a far worse failure than the timeout itself.
    """
    if existed_before:
        return
    try:
        if output_path.exists():
            output_path.unlink()
    except OSError:
        pass


async def _wait_for_exit(proc: Any, timeout: float) -> bool:
    """Wait up to *timeout* seconds for *proc* to be reaped; True when it exited."""
    try:
        await asyncio.wait_for(proc.wait(), timeout)
        return True
    except asyncio.TimeoutError:
        return False
    except (AttributeError, OSError, ValueError):
        # An object that cannot be waited on (a stub, an already-reaped child)
        # leaves nothing running that this function could usefully wait for.
        return True


def _process_group_id(proc: Any) -> Optional[int]:
    """The child's process-group id, or None when it cannot be read."""
    try:
        return os.getpgid(proc.pid)
    except (AttributeError, OSError):
        return None


def _signal_process_group(proc: Any, pgid: Optional[int], sig: int) -> None:
    """Signal the whole render group, falling back to the direct child."""
    try:
        if pgid is None:
            proc.send_signal(sig)
        else:
            os.killpg(pgid, sig)
    except (AttributeError, OSError):
        pass


async def _kill_posix_process_tree(proc: Any, *, grace: float = _KILL_GRACE_SECONDS) -> None:
    """SIGTERM the render's whole process group, then SIGKILL what is left.

    The render is ``npx`` plus several ``chrome-headless-shell`` children plus
    ffmpeg. Signalling only the direct child orphans the browser and leaves
    ffmpeg encoding into a file nobody is waiting for; the spawn site starts the
    child in its own session precisely so ``os.killpg`` can reach all of them.
    The group id is read rather than assumed to equal the pid.
    """
    pgid = _process_group_id(proc)
    _signal_process_group(proc, pgid, signal.SIGTERM)
    if await _wait_for_exit(proc, grace):
        return
    _signal_process_group(proc, pgid, signal.SIGKILL)
    await _wait_for_exit(proc, _KILL_GRACE_SECONDS)


async def _run_taskkill(proc: Any) -> None:
    """Run ``taskkill /PID <pid> /T /F`` hidden; never raises.

    ``/T`` walks the process tree, which is the whole point: ``Process.kill()``
    on Windows terminates only the process it was handed. ``/F`` is required
    because chrome does not honour a console terminate. Async so the event loop
    keeps running while the render is being torn down.
    """
    command = ["taskkill", "/PID", str(proc.pid), "/T", "/F"]
    try:
        # CREATE_NO_WINDOW keeps a console-less server from flashing a window;
        # getattr keeps the call constructible (and testable) off Windows.
        killer = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        await asyncio.wait_for(killer.wait(), _TASKKILL_TIMEOUT)
    except (AttributeError, OSError, ValueError, asyncio.TimeoutError):
        pass


async def _kill_windows_process_tree(proc: Any, *, grace: float = _KILL_GRACE_SECONDS) -> None:
    """Graceful ``terminate()`` first, then ``taskkill /T /F`` for the tree."""
    try:
        proc.terminate()
    except (AttributeError, OSError):
        pass
    if await _wait_for_exit(proc, grace):
        return
    await _run_taskkill(proc)
    await _wait_for_exit(proc, _KILL_GRACE_SECONDS)


def _force_kill_process(proc: Any) -> None:
    """Synchronous backstop: SIGKILL the group (POSIX) or the child (Windows).

    Runs from :func:`_kill_process_tree`'s ``finally`` because the awaits in the
    platform helpers can be cancelled a second time during a shutdown, and a
    kill that never landed is precisely the leak this exists to prevent. On
    Windows only the direct child can be signalled synchronously, but by then
    ``taskkill /T`` has already had its turn at the tree.
    """
    if proc is None or proc.returncode is not None:
        return
    try:
        if _is_windows():
            proc.kill()
            return
        pgid = _process_group_id(proc)
        if pgid is None:
            proc.kill()
        else:
            os.killpg(pgid, signal.SIGKILL)
    except (AttributeError, OSError):
        pass


async def _kill_process_tree(proc: Any, *, grace: float = _KILL_GRACE_SECONDS) -> None:
    """Terminate the render and everything it spawned, then reap it.

    The platform choice goes through :func:`_is_windows` rather than an inline
    ``os.name`` check so both branches are reachable from a Linux test run.
    Returns immediately for a process that has already exited.
    """
    if proc is None or proc.returncode is not None:
        return
    try:
        if _is_windows():
            await _kill_windows_process_tree(proc, grace=grace)
        else:
            await _kill_posix_process_tree(proc, grace=grace)
    finally:
        _force_kill_process(proc)


async def render_with_hyperframes(
    project_name: str,
    composition_html: str,
    output_path: Path,
    project_dir: Path,
    width: int,
    height: int,
) -> Dict[str, Any]:
    """Run the HyperFrames render under a watchdog and reap it either way.

    The wait for the subprocess used to be a bare ``await proc.communicate()``
    with no bound and no cancellation handling. A render legitimately needs
    minutes (~316s for a 41s 1080x1920 cut here), so a client that walked away
    left chrome and ffmpeg busy for the full run, two renders ended up racing for
    the same project files, and the server could not shut down. Now the wait is
    bounded by :func:`render_timeout_seconds` and both the timeout and
    ``asyncio.CancelledError`` kill the whole tree - see
    :func:`_kill_process_tree` - so nothing is left running and no half-written
    MP4 is ever reported as ``status="rendered"``.
    """
    (project_dir / "index.html").write_text(composition_html, encoding="utf-8")

    timeout = render_timeout_seconds()
    # Remembered so an abandoned render can drop the MP4 it half-wrote without
    # ever deleting a previous good render of the same project.
    output_existed = output_path.exists()

    try:
        # exec with an argument list, not a shell string: no quoting surprises.
        spawn_kwargs: Dict[str, Any] = {}
        if not _is_windows():
            # Its own session makes npx a process-group leader, which is the
            # only handle that reaches chrome-headless-shell and ffmpeg later.
            # POSIX-only: Windows rejects start_new_session outright.
            spawn_kwargs["start_new_session"] = True
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
            **spawn_kwargs,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError as exc:
            await _kill_process_tree(proc)
            # A run that actually finished inside the race window keeps its file;
            # one the watchdog cut short does not, because a truncated MP4 that
            # looks like a deliverable is worse than no file at all.
            if proc.returncode != 0:
                _discard_partial_output(output_path, output_existed)
            raise RuntimeError(
                f"tempo limite de render excedido ({timeout:.0f}s). O render foi "
                f"encerrado; reduza a duracao do video ou aumente "
                f"{_RENDER_TIMEOUT_ENV}."
            ) from exc
        except asyncio.CancelledError:
            # CancelledError derives from BaseException, so the caller's
            # "except Exception" never sees it - and must not. Kill the tree and
            # re-raise: swallowing a shutdown would leave chrome running.
            await _kill_process_tree(proc)
            if proc.returncode != 0:
                _discard_partial_output(output_path, output_existed)
            raise

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


def _has_audio_stream(video_path: Path) -> Optional[bool]:
    """True/False when ffprobe could answer, None when it could not.

    The distinction matters: a video with no audio track must get the music as
    its sole audio rather than a failed mix, but a probe that simply could not
    run must not be mistaken for "no audio" - the caller retries that case with
    the audio graph first.
    """
    try:
        probe = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "a",
                "-show_entries", "stream=codec_type",
                "-of", "csv=p=0",
                str(video_path),
            ],
            capture_output=True,
            text=True,
            timeout=_FFPROBE_TIMEOUT,
        )
    except (OSError, ValueError, SubprocessError):
        return None
    if probe.returncode != 0:
        return None
    return any(line.strip() for line in (probe.stdout or "").splitlines())


def _music_chain(params: Dict[str, Any], duration: float) -> str:
    """The music branch of the mix graph: level, filter and edge fades.

    The library peak-normalises every track to -3 dBFS, so a linear
    ``music_volume`` of 0.18 lands the bed near the -18 dBFS target the mixer
    contract names. The 8 kHz lowpass keeps it a bed rather than a lead, and the
    fades mean it never starts or stops abruptly under the voice.
    """
    volume = float(params["music_volume"])
    fade_in = min(float(params["fade_in"]), max(0.0, duration) * 0.4)
    fade_out = min(float(params["fade_out"]), max(0.0, duration) * 0.4)
    fade_start = max(0.0, max(0.0, duration) - fade_out)
    return (
        f"volume={volume:.4f},"
        f"{params['filter']},"
        f"afade=t=in:st=0:d={fade_in:.3f},"
        f"afade=t=out:st={fade_start:.3f}:d={fade_out:.3f}"
    )


def _ducked_mix_filter(params: Dict[str, Any], duration: float) -> str:
    """Full two-input graph: the music bed ducked by the narration.

    One ffmpeg call, one filter graph. The voice is split because it is both the
    mix input and the sidechain key; ``normalize=0`` on the amix keeps the voice
    at its own level instead of halving both stems, and the trailing alimiter
    puts the mix ceiling at the contract's voice target so nothing clips.
    """
    ceiling = 10 ** (float(params["voice_target_db"]) / 20.0)
    graph = (
        f"[0:a]asplit=2[voice][key];"
        f"[1:a]{_music_chain(params, duration)}[bed];"
        # Sidechain keys off the voice, so the music dips under speech and
        # recovers in the gaps: threshold 0.05 linear (~-26 dBFS), 8:1.
        f"[bed][key]sidechaincompress=threshold=0.05:ratio=8:attack=15:release=350[ducked];"
        f"[voice][ducked]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[mixed];"
        f"[mixed]alimiter=limit={ceiling:.4f}[out]"
    )
    return graph


def _flat_mix_filter(params: Dict[str, Any], duration: float) -> str:
    """Two-input graph with no ducking: voice and bed summed at their own levels.

    The voice is NOT split here. Nothing keys off it when ``duck_voice`` is off,
    and a second ``asplit`` output left unconsumed makes ffmpeg abort with
    "Filter asplit has an unconnected output" (a fatal error, since every
    labelled filter output must be consumed by another filter or by ``-map``).
    That failure was silent end to end: the non-zero exit fell into the
    mixer's error path, which returned the original video and reported the bed
    as "ffmpeg indisponivel ou mixagem falhou".
    """
    ceiling = 10 ** (float(params["voice_target_db"]) / 20.0)
    return (
        f"[0:a]anull[voice];"
        f"[1:a]{_music_chain(params, duration)}[bed];"
        f"[voice][bed]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[mixed];"
        f"[mixed]alimiter=limit={ceiling:.4f}[out]"
    )


def _music_only_filter(params: Dict[str, Any], duration: float) -> str:
    """Graph for a video with no audio track: the music becomes the audio."""
    ceiling = 10 ** (float(params["voice_target_db"]) / 20.0)
    return f"{_music_chain(params, duration)},alimiter=limit={ceiling:.4f}"


def mix_audio_track(
    video_path: Path,
    music_path: Optional[Path],
    output_path: Path,
    *,
    music_volume: float = music.DEFAULT_MUSIC_VOLUME,
    duck_voice: bool = True,
    total_duration: Optional[float] = None,
) -> Path:
    """Mix a music bed under a rendered video's existing audio, in one ffmpeg call.

    HyperFrames has already muxed the narration into the MP4, so the bed is a
    post-process: levels come from ``music.mix_parameters`` and the track is
    stretched to cover the video with ``music.loop_to_duration`` so a 40s track
    still beds a 90s cut. With ``duck_voice`` the music is keyed off the narration
    through ``sidechaincompress``; a video with no audio track gets the music as
    its sole audio instead of a failed mix.

    Video is stream-copied, never re-encoded. Every failure - no ffmpeg, no
    music, a non-zero exit, an unwritable output - returns the ORIGINAL
    ``video_path`` untouched: music is a bonus, never a reason to lose a render.
    """
    video = Path(video_path)
    output = Path(output_path)
    if music_path is None:
        return video
    track = Path(music_path)
    if not video.exists() or not track.exists():
        return video

    params = music.mix_parameters(music_volume, duck_voice)
    if float(params["music_volume"]) <= 0.0:
        # Zero means "no bed", which is exactly what the caller already has.
        return video

    duration = 0.0
    for candidate in (total_duration, get_media_duration(video)):
        try:
            duration = float(candidate or 0.0)
        except (TypeError, ValueError):
            duration = 0.0
        if duration > 0.0:
            break

    has_audio = _has_audio_stream(video)
    plans: List[List[str]] = []
    if has_audio is not False:
        # Also the fallback for an unprobeable file: tried first, retried without
        # audio below, so a missing stream costs one extra call at worst.
        graph = (
            _ducked_mix_filter(params, duration)
            if params["duck_voice"]
            else _flat_mix_filter(params, duration)
        )
        plans.append([
            "ffmpeg", "-v", "error", "-y",
            "-i", str(video), "-i", str(track),
            "-filter_complex", graph,
            "-map", "0:v:0", "-map", "[out]",
            "-c:v", "copy", "-c:a", "aac", "-b:a", _MIX_AUDIO_BITRATE,
            "-shortest", str(output),
        ])
    if has_audio is not True:
        plans.append([
            "ffmpeg", "-v", "error", "-y",
            "-i", str(video), "-i", str(track),
            "-map", "0:v:0", "-map", "1:a:0",
            "-af", _music_only_filter(params, duration),
            "-c:v", "copy", "-c:a", "aac", "-b:a", _MIX_AUDIO_BITRATE,
            "-shortest", str(output),
        ])

    for command in plans:
        # Written to a sibling temp file and moved into place, so a failed mix can
        # never leave a truncated file where the caller expects the output.
        staged = output.with_name(output.stem + ".mixing" + output.suffix)
        command[-1] = str(staged)
        try:
            subprocess.run(command, check=True, capture_output=True, text=True,
                           timeout=_MIX_TIMEOUT)
            if not staged.exists() or staged.stat().st_size == 0:
                continue
            output.parent.mkdir(parents=True, exist_ok=True)
            os.replace(str(staged), str(output))
            return output
        except (OSError, ValueError, SubprocessError):
            try:
                staged.unlink()
            except OSError:
                pass
            continue
    return video


def _apply_background_music(
    video_path: Path,
    music_track: Optional[str],
    music_volume: float,
    duck_voice: bool,
    total_duration: float,
) -> tuple[Path, Dict[str, Any]]:
    """Mix *music_track* into *video_path* and report what happened.

    Returns the path to use (the original when nothing was mixed) plus the
    ``music`` payload block. Never raises: an unknown track id, an unusable file
    or a missing ffmpeg all come back as ``applied: False`` with a note naming
    the reason, because a render that succeeded must never be thrown away over a
    missing background bed.

    ``note`` is never empty on any path that did not mix: a video with no bed is
    a result worth explaining, and an empty string explained nothing.
    """
    report: Dict[str, Any] = {
        "requested": music_track or None,
        "applied": False,
        "track_id": None,
        "volume": float(music_volume),
        "duck_voice": bool(duck_voice),
        "note": "",
    }
    if not music_track:
        # Auto-BGM wanted a bed and music.pick_track could not produce one, so
        # the video ships silent. Reporting applied=False with an empty note left
        # the user with a silent video and no explanation; the wording lives next
        # to the library that failed to appear, so there is one phrasing of this
        # failure rather than two.
        report["note"] = music.missing_library_note()
        return video_path, report

    try:
        track = music.get_track(music_track)
        if track is None:
            # storage/music/ is gitignored, so a fresh checkout holds no WAVs at
            # all and the pure read above answers None for a built-in id that is
            # in the library: the track was never built, and building it is
            # exactly what the library is for. Healing it here is what the music
            # endpoints do before their read and what pick_track does for
            # auto-BGM; get_track stays a pure read on purpose, because it also
            # answers "does this id exist?", where writing a file would be a lie.
            # Idempotent: once the library is on disk this costs one stat per
            # spec. Retried once, so a genuinely unknown id still falls through
            # to the honest note below.
            music.ensure_builtin_library()
            track = music.get_track(music_track)
    except Exception:  # pragma: no cover - music.py is total, belt and braces
        track = None
    if track is None:
        report["note"] = f"musica ignorada: trilha '{music_track}' nao encontrada."
        return video_path, report

    report["track_id"] = track.id
    source = Path(track.path)
    if not source.exists():
        report["note"] = f"musica ignorada: arquivo da trilha '{track.id}' indisponivel."
        return video_path, report

    try:
        with tempfile.TemporaryDirectory(dir=str(OUTPUT_DIR)) as tmpdir:
            tmp = Path(tmpdir)
            # loop_to_duration covers a track shorter than the cut with one
            # ffmpeg call and degrades to a copy when the track is long enough.
            bed = music.loop_to_duration(source, total_duration, tmp / "music-bed.wav")
            mixed = mix_audio_track(
                video_path,
                Path(bed),
                tmp / "mixed.mp4",
                music_volume=music_volume,
                duck_voice=duck_voice,
                total_duration=total_duration,
            )
            if mixed == video_path or not Path(mixed).exists():
                report["note"] = (
                    "musica nao aplicada: ffmpeg indisponivel ou mixagem falhou; "
                    "o video foi mantido sem trilha."
                )
                return video_path, report
            os.replace(str(mixed), str(video_path))
    except Exception as exc:  # noqa: BLE001 - the render must survive anything
        report["note"] = f"musica nao aplicada: {exc}"
        return video_path, report

    report["applied"] = True
    report["note"] = f"trilha '{track.id}' misturada em {total_duration:.1f}s."
    return video_path, report


async def render_video_hyperframes(
    project_name: str,
    srt_path: Path,
    storyboard: List[Dict[str, Any]] | None = None,
    audio_path: Path | None = None,
    aspect_ratio: str = "vertical",
    output_stem: str | None = None,
    media_pool: Optional[List[str]] = None,
    *,
    preset: Optional[str] = None,
    subtitle_style: Optional[dict] = None,
    music_track: Optional[str] = None,
    music_volume: float = music.DEFAULT_MUSIC_VOLUME,
    duck_voice: bool = True,
) -> Dict[str, Any]:
    """Render a project to MP4.

    ``output_stem`` keeps derived renders (e.g. the shorts cut) from overwriting the
    main video for the same project.

    ``preset``/``subtitle_style`` shape the composition; ``music_track`` (an id
    from ``music.list_tracks``), ``music_volume`` and ``duck_voice`` shape a
    background bed mixed in after HyperFrames has produced the MP4. Music is
    strictly a post-process and never fatal: a missing track or a missing ffmpeg
    still returns ``status="rendered"`` with ``music.applied = False`` and a note
    explaining why. The payload always carries ``preset``, the resolved
    ``subtitle_style`` and the ``music`` block, on success and on error alike.

    A render that outruns ``DARK_STUDIO_RENDER_TIMEOUT`` is killed and reported
    like any other failure - ``status="error"`` carrying a Portuguese message -
    never as a half-finished video.
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

    # Resolved once here so the reported style is exactly the one the
    # composition was built from; resolution is pure, so the call inside
    # generate_composition_html reaches the same answer.
    resolved_preset, resolved_style = resolve_preset_and_style(preset, subtitle_style)

    composition_html = generate_composition_html(
        stem, storyboard, media_refs, audio_ref, aspect_ratio, total_duration,
        preset=preset, subtitle_style=subtitle_style,
    )

    width, height = get_dimensions(aspect_ratio)

    try:
        result = await render_with_hyperframes(
            stem, composition_html, output_path, project_dir, width, height
        )
        cleanup_render_dirs(keep=project_dir)

        video_duration = get_media_duration(output_path)

        # The music stage runs only after a successful render: the narration is
        # already muxed into the MP4 by HyperFrames, so the bed is laid over the
        # finished file and the result replaces it atomically.
        output_path, music_report = _apply_background_music(
            output_path, music_track, music_volume, duck_voice,
            float(video_duration or 0.0) or float(total_duration or 0.0),
        )
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
            "preset": resolved_preset.name,
            "subtitle_style": resolved_style.to_dict(),
            "music": music_report,
        }
    except Exception as e:
        payload = {
            "project_name": project_name,
            "output_stem": stem,
            "status": "error",
            "error": str(e),
            "message": f"Falha no render HyperFrames: {e}",
            "preset": resolved_preset.name,
            "subtitle_style": resolved_style.to_dict(),
            "music": {
                "requested": music_track or None,
                "applied": False,
                "track_id": None,
                "volume": float(music_volume),
                "duck_voice": bool(duck_voice),
                "note": "musica nao aplicada: o render falhou antes da mixagem.",
            },
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