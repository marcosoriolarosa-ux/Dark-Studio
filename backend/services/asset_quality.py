"""Measure border-strip statistics with ffmpeg to find padded stock images.

Some stock providers return images with a large uniform white canvas around the
real photo (observed: a 1880x1246 JPEG that was ~45% white). object-fit: cover
faithfully renders that white block, so the video looks broken.

ffmpeg is already a dependency. Two signalstats passes per asset (one full frame,
one border strip) give the numbers to threshold, and the verdict is cached by
content hash so each unique asset is measured once.
"""
import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Dict, Optional, Tuple

_FULL_RE = re.compile(r"lavfi\.signalstats\.YAVG=(\d+\.?\d*)")
_MIN_RE = re.compile(r"lavfi\.signalstats\.YMIN=(\d+\.?\d*)")
_MAX_RE = re.compile(r"lavfi\.signalstats\.YMAX=(\d+\.?\d*)")
_DIM_RE = re.compile(r"Video: .*?, (\d+)x(\d+)")


def _run_stats(image: Path, crop: Optional[str]) -> Dict[str, float]:
    """Return YAVG/YMIN/YMAX for the whole frame or for a crop region."""
    filters = f"crop={crop}," if crop else ""
    cmd = [
        "ffmpeg", "-v", "error", "-i", str(image),
        "-vf", f"{filters}signalstats,metadata=print:file=-",
        "-frames:v", "1", "-f", "null", "-",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    text = result.stdout + result.stderr
    out = {}
    for key, pattern in (("yavg", _FULL_RE), ("ymin", _MIN_RE), ("ymax", _MAX_RE)):
        match = pattern.search(text)
        if match:
            out[key] = float(match.group(1))
    return out


def image_dimensions(image: Path) -> Optional[Tuple[int, int]]:
    """Width/height via ffprobe.

    ``-of csv=p=0`` prints ``1880,1246``; the s= variant prints ``1880x1246``.
    Accept both rather than assuming one.
    """
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", str(image)],
        capture_output=True, text=True,
    )
    for pattern in (r"(\d+)[x,](\d+)", _DIM_RE):
        match = re.search(pattern, result.stdout.strip())
        if match:
            return (int(match.group(1)), int(match.group(2)))
    return None


# Padding detection thresholds, tuned against observed provider results.
_BRIGHT_LUMA = 235.0        # near-white
_MAX_STRIP_SPREAD = 14.0    # uniform within this luma range
_MIN_CENTRE_CONTRAST = 45.0 # the strip is much brighter than the whole frame
_SHALLOW = 0.12             # first probe, this fraction of the shorter side
_DEEP = 0.35                # second probe; if still uniform, padding is substantial

_VERDICT_CACHE: Dict[str, bool] = {}


def looks_padded(image: Path) -> bool:
    """True when a large uniform bright band borders an otherwise darker image."""
    if not image.exists() or image.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp", ".bmp"}:
        return False

    digest = hashlib.sha256(image.read_bytes()).hexdigest()
    cached = _VERDICT_CACHE.get(digest)
    if cached is not None:
        return cached

    verdict = _measure_padding(image)
    _VERDICT_CACHE[digest] = verdict
    return verdict


def _is_uniform_bright(stats: Dict[str, float]) -> bool:
    if not {"yavg", "ymin", "ymax"} <= stats.keys():
        return False
    return stats["yavg"] >= _BRIGHT_LUMA and (stats["ymax"] - stats["ymin"]) <= _MAX_STRIP_SPREAD


def _measure_padding(image: Path) -> bool:
    """Probe each edge twice: a shallow strip, then a deeper one.

    Depth is what matters, not strip area. A thin uniform strip only proves the edge
    is flat; if the region 35% in is still flat and bright, the asset is padded.
    """
    dims = image_dimensions(image)
    if not dims:
        return False
    width, height = dims

    full = _run_stats(image, None)
    if "yavg" not in full or full["yavg"] >= _BRIGHT_LUMA:
        # An overall bright frame is a high-key photo, not a padded canvas.
        return False

    shallow = max(8, int(min(width, height) * _SHALLOW))
    deep = max(shallow + 8, int(min(width, height) * _DEEP))

    edges = (
        ("right", shallow, deep, width, height, True),
        ("left", shallow, deep, width, height, False),
        ("bottom", deep, shallow, width, height, True),
        ("top", deep, shallow, width, height, False),
    )

    for label, near, far, box_w, box_h, horizontal in edges:
        near_crop, far_crop = _edge_crops(label, box_w, box_h, near, far, horizontal)
        near_stats = _run_stats(image, near_crop)
        if not _is_uniform_bright(near_stats):
            continue
        if near_stats["yavg"] - full["yavg"] < _MIN_CENTRE_CONTRAST:
            continue  # whole image is bright; not padding
        if _is_uniform_bright(_run_stats(image, far_crop)):
            return True

    return False


def _edge_crops(label, box_w, box_h, near, far, horizontal):
    """Crop expressions for the shallow and deep probes of one edge."""
    if label == "right":
        return f"{near}:{box_h}:{max(0, box_w - near)}:0", f"{far}:{box_h}:{max(0, box_w - far)}:0"
    if label == "left":
        return f"{near}:{box_h}:0:0", f"{far}:{box_h}:0:0"
    if label == "bottom":
        return f"{box_w}:{near}:0:{max(0, box_h - near)}", f"{box_w}:{far}:0:{max(0, box_h - far)}"
    return f"{box_w}:{near}:0:0", f"{box_w}:{far}:0:0"


def describe(image: Path) -> Dict[str, object]:
    """Diagnostics for the API/UI: why an asset was kept or dropped."""
    dims = image_dimensions(image)
    return {
        "file": image.name,
        "width": dims[0] if dims else None,
        "height": dims[1] if dims else None,
        "padded": looks_padded(image),
        "full_luma": round(_run_stats(image, None).get("yavg", 0.0), 1),
    }


if __name__ == "__main__":
    import sys

    for arg in sys.argv[1:]:
        path = Path(arg)
        print(json.dumps(describe(path), ensure_ascii=False))