"""Subtitle style and theme preset schema.

The render engine used to hardcode every caption decision: one 52px white face,
one weight, one position vocabulary. This module turns those decisions into a
typed, validated value object that both the composition generator and the WebUI
can speak, and ships the preset catalogue those values come from.

Two rules shape everything here.

1. ``to_css()`` output is interpolated raw into the composition's ``<style>``
   block, so every value it returns must be safe by construction. Colours come
   from a strict ``#RRGGBB`` allowlist and font families from a fixed allowlist
   of bare family names; there is no path from user input to an arbitrary CSS
   token. The renderer's CSS parser is also fragile with encoded strings - an
   SVG data-URI in a ``url()`` trips it ("Unclosed string" on the encoded
   quotes) - so no value here may ever be a data-URI, and explanatory comments
   belong in the generated HTML around this block, never inside a value.
2. Every parser is total. ``from_dict`` and the preset getters accept hostile
   input without raising; a bad field degrades to the default for that field
   alone. A malformed style request must never fail a render.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Dict, List, Optional, Tuple

# Vertical placement of the caption block. The renderer maps these onto the
# `.caption` container's align-items, so the vocabulary is fixed.
VALID_POSITIONS: Tuple[str, ...] = ("top", "upper", "center", "lower", "bottom")

# Caption behaviour. These are the existing render modes; "hidden" still
# suppresses the caption entirely even when a style is configured.
VALID_CAPTION_MODES: Tuple[str, ...] = ("bottom", "center", "hook", "karaoke", "hidden")

# Only bare, well-known family names. A family reaches the CSS unquoted, so the
# allowlist is also the injection boundary: no quotes, commas, semicolons,
# braces, backslashes or angle brackets can appear in a valid entry.
VALID_FONT_FAMILIES: Tuple[str, ...] = (
    "Inter",
    "Montserrat",
    "Poppins",
    "Roboto Condensed",
    "Bebas Neue",
    "Anton",
    "Archivo Black",
    "Playfair Display",
    "Oswald",
    "Lora",
    "Impact",
    "Georgia",
    "System",
    "Segoe UI",
    "Arial",
    "Courier New",
)

VALID_TRANSITIONS: Tuple[str, ...] = ("fade", "wipe", "slide_left", "cut")
VALID_EFFECTS: Tuple[str, ...] = ("cinematic", "glow", "clean")

DEFAULT_PRESET = "cinematic"

# Numeric bounds. Font size is a pixel value in a 1080-wide composition: below
# ~18px a caption is unreadable on a phone, above ~160px it cannot wrap two
# words per line at 84% width.
FONT_SIZE_MIN = 18
FONT_SIZE_MAX = 160
STROKE_WIDTH_MIN = 0
STROKE_WIDTH_MAX = 20
MAX_WIDTH_PERCENT_MIN = 40
MAX_WIDTH_PERCENT_MAX = 100

# Exactly six hex digits. Named colours, 3-digit shorthand and rgb()/hsl()
# functions are all rejected: the shorthand and the function forms are both
# places a crafted value can smuggle a second declaration past a naive check.
_HEX_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
_FAMILY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9 -]{0,40}$")

# Per-position container alignment, so the renderer can map a style's position
# onto `.caption` without duplicating the table.
_POSITION_ALIGN: Dict[str, str] = {
    "top": "flex-start",
    "upper": "center",
    "center": "center",
    "lower": "center",
    "bottom": "flex-end",
}


def is_valid_color(value: object) -> bool:
    """True only for a bare ``#RRGGBB`` string, in either case."""
    return isinstance(value, str) and bool(_HEX_COLOR_RE.match(value))


def is_valid_font_family(value: object) -> bool:
    """True only for a family on the allowlist, matched case-insensitively."""
    if not isinstance(value, str):
        return False
    candidate = value.strip()
    folded = candidate.casefold()
    if folded not in {family.casefold() for family in VALID_FONT_FAMILIES}:
        return False
    # The allowlist is the primary guard; this is the belt-and-braces check
    # that the value is a bare family name safe to emit unquoted.
    return bool(_FAMILY_RE.match(candidate))


def valid_color(value: object, fallback: str) -> str:
    """Trim surrounding whitespace, then accept only ``#RRGGBB``.

    ``is_valid_color`` stays strict about the raw string; the parser is lenient
    about padding, because a stray space in a settings form is a typo and not an
    attack, while everything inside the value is still fully validated.
    """
    if not isinstance(value, str):
        return fallback
    candidate = value.strip()
    return candidate if _HEX_COLOR_RE.match(candidate) else fallback


def valid_font_family(value: object, fallback: str) -> str:
    """Return the canonical allowlist spelling, or ``fallback``."""
    if not is_valid_font_family(value):
        return fallback
    folded = value.strip().casefold()
    for family in VALID_FONT_FAMILIES:
        if family.casefold() == folded:
            return family
    return fallback


def valid_choice(value: object, allowed: Tuple[str, ...], fallback: str) -> str:
    if not isinstance(value, str):
        return fallback
    candidate = value.strip().casefold()
    return candidate if candidate in allowed else fallback


def valid_bool(value: object, fallback: bool) -> bool:
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
    return fallback


def valid_int(value: object, fallback: int, low: int, high: int) -> int:
    """Clamp an integer into ``[low, high]``; non-integers take the fallback.

    ``bool`` is rejected explicitly: it is an ``int`` subclass, so a stray
    ``True`` would otherwise become a size of 1 and clamp to the minimum.
    """
    if isinstance(value, bool):
        return fallback
    if isinstance(value, int):
        number = value
    elif isinstance(value, float):
        if number_is_unusable(value):
            return fallback
        number = int(value)
    elif isinstance(value, str):
        try:
            number = int(value.strip())
        except (TypeError, ValueError):
            return fallback
    else:
        return fallback
    return max(low, min(high, number))


def number_is_unusable(value: float) -> bool:
    """True for NaN and the infinities, which cannot survive a clamp."""
    return value != value or value in (float("inf"), float("-inf"))


def valid_float(value: object, fallback: float, low: float, high: float) -> float:
    """Clamp a float into ``[low, high]``; non-numbers take the fallback."""
    if isinstance(value, bool):
        return fallback
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except (TypeError, ValueError):
            return fallback
    else:
        return fallback
    if number != number:  # NaN
        return fallback
    return max(low, min(high, number))


def hex_to_rgb(color: str) -> Tuple[int, int, int]:
    """Parse a validated ``#RRGGBB`` colour into its three channels."""
    value = color.lstrip("#")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def rgba(color: str, alpha: float) -> str:
    """Build an ``rgba()`` string from a validated colour and a clamped alpha.

    No spaces after the commas: the renderer interpolates this straight into
    the style block, and the compact form matches the literals already there.
    """
    red, green, blue = hex_to_rgb(color)
    return f"rgba({red},{green},{blue},{round(alpha, 3)})"


def align_for_position(position: str) -> str:
    """Container alignment for a caption position, defaulting to the bottom."""
    return _POSITION_ALIGN.get(position, _POSITION_ALIGN["bottom"])


@dataclass
class SubtitleStyle:
    """A fully specified caption look.

    The field defaults are the look the renderer used to hardcode, so an
    unconfigured project renders exactly as it did before this module existed.
    Instances are not self-validating: build one through ``from_dict`` or call
    ``normalised()`` before serialising, since only those enforce the bounds.
    """

    font_family: str = "Inter"
    font_size: int = 52
    primary_color: str = "#FFFFFF"
    stroke_color: str = "#000000"
    stroke_width: int = 4
    shadow: bool = True
    background_opacity: float = 0.0
    background_color: str = "#05070B"
    position: str = "bottom"
    uppercase: bool = False
    bold: bool = True
    max_width_percent: int = 84
    mode: str = "bottom"

    def normalised(self) -> "SubtitleStyle":
        """Return a clamped, valid copy. Idempotent, and never mutates self.

        Every field is re-validated against the same defaults the dataclass
        declares, so hand-built and deserialised styles converge on one value.
        """
        return SubtitleStyle(
            font_family=valid_font_family(self.font_family, "Inter"),
            font_size=valid_int(self.font_size, 52, FONT_SIZE_MIN, FONT_SIZE_MAX),
            primary_color=valid_color(self.primary_color, "#FFFFFF"),
            stroke_color=valid_color(self.stroke_color, "#000000"),
            stroke_width=valid_int(
                self.stroke_width, 4, STROKE_WIDTH_MIN, STROKE_WIDTH_MAX
            ),
            shadow=valid_bool(self.shadow, True),
            background_opacity=valid_float(self.background_opacity, 0.0, 0.0, 1.0),
            background_color=valid_color(self.background_color, "#05070B"),
            position=valid_choice(self.position, VALID_POSITIONS, "bottom"),
            uppercase=valid_bool(self.uppercase, False),
            bold=valid_bool(self.bold, True),
            max_width_percent=valid_int(
                self.max_width_percent, 84, MAX_WIDTH_PERCENT_MIN, MAX_WIDTH_PERCENT_MAX
            ),
            mode=valid_choice(self.mode, VALID_CAPTION_MODES, "bottom"),
        )

    def to_dict(self) -> dict:
        """The normalised style as plain JSON-ready data."""
        style = self.normalised()
        return {
            "font_family": style.font_family,
            "font_size": style.font_size,
            "primary_color": style.primary_color,
            "stroke_color": style.stroke_color,
            "stroke_width": style.stroke_width,
            "shadow": style.shadow,
            "background_opacity": style.background_opacity,
            "background_color": style.background_color,
            "position": style.position,
            "uppercase": style.uppercase,
            "bold": style.bold,
            "max_width_percent": style.max_width_percent,
            "mode": style.mode,
        }

    def to_css(self) -> dict:
        """CSS declarations for this style, safe to interpolate into a rule.

        Values are literal strings with no braces, semicolons, newlines, quotes
        or ``url()`` payloads; the font family is a bare allowlisted name. A
        caller may join this as ``"; ".join(f"{k}: {v}" for k, v in css.items())``
        without further escaping. The renderer owns placement and the caption
        box geometry, so this covers the text node only - pair it with
        ``align_for_position(style.position)`` for the container.
        """
        style = self.normalised()
        return {
            "font-family": style.font_family,
            "font-size": f"{style.font_size}px",
            "font-weight": "800" if style.bold else "400",
            "line-height": "1.18",
            "letter-spacing": "normal",
            "color": style.primary_color,
            "-webkit-text-stroke": f"{style.stroke_width}px {style.stroke_color}",
            "paint-order": "stroke fill",
            "text-shadow": "0 4px 14px rgba(0,0,0,0.85)" if style.shadow else "none",
            "text-transform": "uppercase" if style.uppercase else "none",
            "max-width": f"{style.max_width_percent}%",
            "background-color": (
                rgba(style.background_color, style.background_opacity)
                if style.background_opacity > 0.0
                else "transparent"
            ),
        }

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "SubtitleStyle":
        """Build a style from untrusted data. Never raises.

        Unknown keys are ignored, a non-dict yields the defaults, and a field
        that fails validation falls back on its own default rather than
        discarding the rest of the payload.
        """
        if not isinstance(data, dict):
            return cls()
        base = cls()
        return cls(
            font_family=valid_font_family(data.get("font_family"), base.font_family),
            font_size=valid_int(
                data.get("font_size"), base.font_size, FONT_SIZE_MIN, FONT_SIZE_MAX
            ),
            primary_color=valid_color(data.get("primary_color"), base.primary_color),
            stroke_color=valid_color(data.get("stroke_color"), base.stroke_color),
            stroke_width=valid_int(
                data.get("stroke_width"),
                base.stroke_width,
                STROKE_WIDTH_MIN,
                STROKE_WIDTH_MAX,
            ),
            shadow=valid_bool(data.get("shadow"), base.shadow),
            background_opacity=valid_float(
                data.get("background_opacity"),
                base.background_opacity,
                0.0,
                1.0,
            ),
            background_color=valid_color(
                data.get("background_color"), base.background_color
            ),
            position=valid_choice(data.get("position"), VALID_POSITIONS, base.position),
            uppercase=valid_bool(data.get("uppercase"), base.uppercase),
            bold=valid_bool(data.get("bold"), base.bold),
            max_width_percent=valid_int(
                data.get("max_width_percent"),
                base.max_width_percent,
                MAX_WIDTH_PERCENT_MIN,
                MAX_WIDTH_PERCENT_MAX,
            ),
            mode=valid_choice(data.get("mode"), VALID_CAPTION_MODES, base.mode),
        ).normalised()


@dataclass
class ThemePreset:
    """A named bundle: a caption style plus the scene treatment it goes with."""

    name: str
    label: str
    subtitle: SubtitleStyle
    palette: List[str] = field(default_factory=list)
    transition: str = "fade"
    effect: str = "cinematic"

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "label": self.label,
            "subtitle": self.subtitle.to_dict(),
            "palette": list(self.palette),
            "transition": self.transition,
            "effect": self.effect,
        }


def _preset(
    name: str,
    label: str,
    subtitle: dict,
    palette: List[str],
    transition: str,
    effect: str,
) -> ThemePreset:
    """Build a preset, dropping any palette entry that is not valid hex."""
    return ThemePreset(
        name=name,
        label=label,
        subtitle=SubtitleStyle.from_dict(subtitle),
        palette=[colour for colour in palette if is_valid_color(colour)],
        transition=valid_choice(transition, VALID_TRANSITIONS, "fade"),
        effect=valid_choice(effect, VALID_EFFECTS, "cinematic"),
    )


# The catalogue. Every palette is at least three hex values the scene
# backgrounds can cycle through, so a project without a background of its own
# still varies between scenes.
_PRESET_LIST: List[ThemePreset] = [
    _preset(
        "cinematic",
        "Cinematic",
        {
            "font_family": "Inter",
            "font_size": 52,
            "primary_color": "#FFFFFF",
            "stroke_color": "#000000",
            "stroke_width": 4,
            "shadow": True,
            "background_opacity": 0.0,
            "position": "bottom",
            "bold": True,
            "max_width_percent": 84,
            "mode": "bottom",
        },
        ["#05070B", "#0F172A", "#1E293B"],
        "fade",
        "cinematic",
    ),
    _preset(
        "bold",
        "Bold Hook",
        {
            "font_family": "Anton",
            "font_size": 78,
            "primary_color": "#FFFFFF",
            "stroke_color": "#000000",
            "stroke_width": 8,
            "shadow": True,
            "background_opacity": 0.0,
            "position": "center",
            "uppercase": True,
            "bold": True,
            "max_width_percent": 88,
            "mode": "hook",
        },
        ["#0B0B0F", "#1A1A22", "#2A2A36"],
        "cut",
        "clean",
    ),
    _preset(
        "minimal",
        "Minimal",
        {
            "font_family": "Inter",
            "font_size": 38,
            "primary_color": "#F1F5F9",
            "stroke_color": "#0F172A",
            "stroke_width": 0,
            "shadow": False,
            "background_opacity": 0.0,
            "position": "lower",
            "uppercase": False,
            "bold": False,
            "max_width_percent": 70,
            "mode": "bottom",
        },
        ["#F8FAFC", "#E2E8F0", "#CBD5E1"],
        "fade",
        "clean",
    ),
    _preset(
        "karaoke",
        "Karaoke",
        {
            "font_family": "Montserrat",
            "font_size": 56,
            "primary_color": "#FFFFFF",
            "stroke_color": "#000000",
            "stroke_width": 3,
            "shadow": True,
            "background_opacity": 0.62,
            "background_color": "#000000",
            "position": "center",
            "uppercase": False,
            "bold": True,
            "max_width_percent": 86,
            "mode": "karaoke",
        },
        ["#101014", "#18181D", "#23232B"],
        "cut",
        "glow",
    ),
    _preset(
        "documentary",
        "Documentary",
        {
            "font_family": "Playfair Display",
            "font_size": 48,
            "primary_color": "#F5F5F4",
            "stroke_color": "#1C1917",
            "stroke_width": 2,
            "shadow": True,
            "background_opacity": 0.0,
            "position": "bottom",
            "uppercase": False,
            "bold": False,
            "max_width_percent": 80,
            "mode": "bottom",
        },
        ["#1C1917", "#292524", "#44403C"],
        "fade",
        "cinematic",
    ),
    _preset(
        "neon",
        "Neon",
        {
            "font_family": "Bebas Neue",
            "font_size": 68,
            "primary_color": "#22D3EE",
            "stroke_color": "#0E7490",
            "stroke_width": 5,
            "shadow": True,
            "background_opacity": 0.0,
            "position": "lower",
            "uppercase": True,
            "bold": True,
            "max_width_percent": 90,
            "mode": "hook",
        },
        ["#04121A", "#062A38", "#0A3D4F"],
        "slide_left",
        "glow",
    ),
    _preset(
        "news",
        "News",
        {
            "font_family": "Inter",
            "font_size": 44,
            "primary_color": "#FFFFFF",
            "stroke_color": "#0F172A",
            "stroke_width": 2,
            "shadow": False,
            "background_opacity": 0.0,
            "position": "upper",
            "uppercase": False,
            "bold": True,
            "max_width_percent": 82,
            "mode": "bottom",
        },
        ["#0B1220", "#152238", "#24354F"],
        "wipe",
        "clean",
    ),
    _preset(
        "podcast",
        "Podcast",
        {
            "font_family": "Poppins",
            "font_size": 60,
            "primary_color": "#F8FAFC",
            "stroke_color": "#000000",
            "stroke_width": 3,
            "shadow": True,
            "background_opacity": 0.72,
            "background_color": "#0A0A0A",
            "position": "bottom",
            "uppercase": False,
            "bold": True,
            "max_width_percent": 80,
            "mode": "bottom",
        },
        ["#121212", "#1C1C1C", "#272727"],
        "fade",
        "clean",
    ),
]

_PRESETS_BY_NAME: Dict[str, ThemePreset] = {preset.name: preset for preset in _PRESET_LIST}


def _copy(preset: ThemePreset, subtitle: Optional[SubtitleStyle] = None) -> ThemePreset:
    """A detached copy, so no caller can mutate the catalogue."""
    return replace(
        preset,
        subtitle=(subtitle or preset.subtitle).normalised(),
        palette=list(preset.palette),
    )


def list_presets() -> List[ThemePreset]:
    """Every preset, in catalogue order."""
    return [_copy(preset) for preset in _PRESET_LIST]


def get_preset(name: str) -> ThemePreset:
    """Look up a preset by slug, falling back to the default. Never raises."""
    key = name.strip().casefold() if isinstance(name, str) else ""
    return _copy(_PRESETS_BY_NAME.get(key, _PRESETS_BY_NAME[DEFAULT_PRESET]))


def preset_names() -> List[str]:
    return [preset.name for preset in _PRESET_LIST]


def resolve_preset_and_style(
    preset: Optional[str], overrides: Optional[dict]
) -> Tuple[ThemePreset, SubtitleStyle]:
    """Resolve a preset name plus per-field overrides into a final style.

    Starts from the named preset (or the default when the name is unknown, blank
    or not a string), applies ``overrides`` on top using ``from_dict`` semantics
    so a bad override degrades to that field's preset value rather than raising,
    and returns a preset carrying the final normalised style. The returned preset
    is a copy; the catalogue is never mutated.
    """
    base = get_preset(preset if isinstance(preset, str) else DEFAULT_PRESET)
    if not isinstance(overrides, dict):
        return base, base.subtitle
    merged = SubtitleStyle.from_dict({**base.subtitle.to_dict(), **overrides})
    return _copy(base, merged), merged
