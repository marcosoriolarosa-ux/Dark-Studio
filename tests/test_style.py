"""Tests for the subtitle style schema and theme preset catalogue.

The schema's whole job is being a safe boundary between untrusted input (a
WebUI form, a settings file, a preset name in a URL) and a raw string
interpolated into the composition's style block. Every test here is therefore
written as either "valid input survives" or "hostile input degrades to the
default instead of raising or escaping into the CSS".
"""
import re

import pytest

from backend.services.style import (
    FONT_SIZE_MAX,
    FONT_SIZE_MIN,
    VALID_CAPTION_MODES,
    VALID_FONT_FAMILIES,
    VALID_POSITIONS,
    SubtitleStyle,
    ThemePreset,
    get_preset,
    is_valid_color,
    is_valid_font_family,
    align_for_position,
    list_presets,
    preset_names,
    resolve_preset_and_style,
)

# Characters that would let a value close its own declaration or rule. None may
# ever appear inside a value from to_css(); only "," "(" ")" "%" "." and "-"
# are expected, in the rgba()/px constructs below.
DANGEROUS = ("{", "}", ";", "\n", "\r", "/*", "*/", '"', "'", "<", ">", "\\", "url(")

HOSTILE_INPUTS = [
    None,
    {},
    {"font_size": "abc"},
    {"font_size": "18; } body { display:none"},
    {"__class__": "x"},
    {"__dict__": {"font_size": 999}},
    [],
    ["font_size"],
    "font_size=999",
    42,
    object(),
    {"primary_color": "red; }body{display:none"},
    {"font_family": "Inter; }body{display:none"},
    {"position": "middle; }"},
    {"mode": None},
    {"background_opacity": "not-a-number"},
    {"max_width_percent": float("nan")},
    {"font_size": float("inf")},
]


class TestDefaults:
    def test_defaults_match_the_previously_hardcoded_look(self):
        style = SubtitleStyle()
        assert style.font_family == "Inter"
        assert style.font_size == 52
        assert style.primary_color == "#FFFFFF"
        assert style.stroke_width == 4
        assert style.shadow is True
        assert style.background_opacity == 0.0
        assert style.position == "bottom"
        assert style.mode == "bottom"

    def test_defaults_are_already_normalised(self):
        assert SubtitleStyle() == SubtitleStyle().normalised()

    def test_normalised_is_idempotent(self):
        once = SubtitleStyle().normalised()
        assert once == once.normalised()
        assert once.normalised().normalised() == once

    def test_normalised_does_not_mutate_the_source(self):
        style = SubtitleStyle(font_size=999, primary_color="red")
        normalised = style.normalised()
        assert style.font_size == 999, "normalised() must leave the input alone"
        assert normalised.font_size == FONT_SIZE_MAX
        assert normalised.primary_color == "#FFFFFF"

    def test_vocabulary_tuples_are_the_documented_ones(self):
        assert VALID_POSITIONS == ("top", "upper", "center", "lower", "bottom")
        assert VALID_CAPTION_MODES == ("bottom", "center", "hook", "karaoke", "hidden")

    def test_required_font_families_are_offered(self):
        required = {
            "Inter", "Montserrat", "Poppins", "Roboto Condensed",
            "Bebas Neue", "Anton", "Archivo Black", "Playfair Display", "System",
        }
        assert required <= set(VALID_FONT_FAMILIES)


class TestClamping:
    def test_font_size_below_the_floor_is_clamped_up(self):
        assert SubtitleStyle(font_size=5).normalised().font_size == FONT_SIZE_MIN

    def test_font_size_above_the_ceiling_is_clamped_down(self):
        assert SubtitleStyle(font_size=999).normalised().font_size == FONT_SIZE_MAX

    def test_font_size_inside_the_range_is_untouched(self):
        assert SubtitleStyle(font_size=72).normalised().font_size == 72

    def test_stroke_width_clamps_at_both_ends(self):
        assert SubtitleStyle(stroke_width=-9).normalised().stroke_width == 0
        assert SubtitleStyle(stroke_width=99).normalised().stroke_width == 20

    def test_background_opacity_clamps_at_both_ends(self):
        assert SubtitleStyle(background_opacity=-2.0).normalised().background_opacity == 0.0
        assert SubtitleStyle(background_opacity=7.5).normalised().background_opacity == 1.0

    def test_max_width_percent_clamps_at_both_ends(self):
        assert SubtitleStyle(max_width_percent=1).normalised().max_width_percent == 40
        assert SubtitleStyle(max_width_percent=400).normalised().max_width_percent == 100

    def test_numeric_strings_are_accepted_and_clamped(self):
        assert SubtitleStyle.from_dict({"font_size": "300"}).font_size == FONT_SIZE_MAX

    def test_a_bool_is_not_a_number(self):
        # True is an int subclass; a stray True must not become size 1.
        assert SubtitleStyle.from_dict({"font_size": True}).font_size == 52


class TestColourValidation:
    @pytest.mark.parametrize("value", ["#ffffff", "#FFFFFF", "#22D3EE", "#05070b"])
    def test_accepts_six_digit_hex_in_either_case(self, value):
        assert is_valid_color(value)

    @pytest.mark.parametrize(
        "value",
        [
            "red",
            "#fff",
            "rgb(0,0,0)",
            "rgba(0,0,0,0.5)",
            "red; }body{display:none",
            "#FFFFFF; }body{display:none",
            "#FFFFF",
            "#GGGGGG",
            "FFFFFF",
            "#FFFFFF ",
            "0xFFFFFF",
            "",
        ],
    )
    def test_rejects_everything_else(self, value):
        assert not is_valid_color(value)

    @pytest.mark.parametrize("value", [None, 0xFFFFFF, ["#FFFFFF"], {"hex": "#FFFFFF"}, b"#FFFFFF"])
    def test_rejects_non_strings(self, value):
        assert not is_valid_color(value)

    def test_invalid_colour_falls_back_and_leaves_other_fields_alone(self):
        style = SubtitleStyle.from_dict(
            {"primary_color": "red; }body{display:none", "font_size": 60}
        )
        assert style.primary_color == "#FFFFFF"
        assert style.font_size == 60, "one bad field must not discard the rest"

    def test_a_padded_colour_is_a_typo_not_an_attack(self):
        assert SubtitleStyle.from_dict({"primary_color": "  #22d3ee  "}).primary_color == "#22d3ee"


class TestFontAllowlist:
    @pytest.mark.parametrize("family", ["Inter", "Montserrat", "Bebas Neue", "Archivo Black"])
    def test_allowlisted_families_pass(self, family):
        assert is_valid_font_family(family)

    def test_allowlist_is_case_insensitive_but_normalises_the_spelling(self):
        assert SubtitleStyle.from_dict({"font_family": "inter"}).font_family == "Inter"

    @pytest.mark.parametrize(
        "family",
        [
            "Inter; }body{display:none",
            "Comic Sans MS",
            "Inter, Arial",
            "url(https://evil.test/x.woff)",
            "Inter\"}",
            "../../etc/passwd",
            "",
        ],
    )
    def test_rejects_unknown_and_injected_families(self, family):
        assert not is_valid_font_family(family)

    @pytest.mark.parametrize("family", [None, 7, ["Inter"]])
    def test_rejects_non_strings(self, family):
        assert not is_valid_font_family(family)

    def test_injected_family_falls_back_to_the_default(self):
        style = SubtitleStyle.from_dict({"font_family": "Inter; }body{display:none"})
        assert style.font_family == "Inter"

    def test_every_allowed_family_emits_a_bare_unquoted_name(self):
        for family in VALID_FONT_FAMILIES:
            css = SubtitleStyle(font_family=family).to_css()
            assert css["font-family"] == family
            for danger in DANGEROUS:
                assert danger not in css["font-family"]


class TestFromDictIsTotal:
    @pytest.mark.parametrize("hostile", HOSTILE_INPUTS)
    def test_hostile_input_never_raises(self, hostile):
        style = SubtitleStyle.from_dict(hostile)
        assert isinstance(style, SubtitleStyle)
        assert style == style.normalised()

    @pytest.mark.parametrize("hostile", HOSTILE_INPUTS)
    def test_hostile_input_never_produces_an_invalid_style(self, hostile):
        style = SubtitleStyle.from_dict(hostile)
        assert FONT_SIZE_MIN <= style.font_size <= FONT_SIZE_MAX
        assert is_valid_color(style.primary_color)
        assert is_valid_color(style.stroke_color)
        assert is_valid_color(style.background_color)
        assert is_valid_font_family(style.font_family)
        assert style.position in VALID_POSITIONS
        assert style.mode in VALID_CAPTION_MODES
        assert 0.0 <= style.background_opacity <= 1.0

    @pytest.mark.parametrize("hostile", HOSTILE_INPUTS)
    def test_hostile_input_never_produces_dangerous_css(self, hostile):
        css = SubtitleStyle.from_dict(hostile).to_css()
        for key, value in css.items():
            for danger in DANGEROUS:
                assert danger not in value, f"{key} leaked {danger!r}"

    def test_none_and_empty_match_the_defaults(self):
        assert SubtitleStyle.from_dict(None) == SubtitleStyle()
        assert SubtitleStyle.from_dict({}) == SubtitleStyle()

    def test_a_list_instead_of_a_dict_yields_the_defaults(self):
        assert SubtitleStyle.from_dict([{"font_size": 100}]) == SubtitleStyle()

    def test_unknown_keys_are_ignored(self):
        style = SubtitleStyle.from_dict({"font_size": 40, "colour": "red", "zzz": object()})
        assert style.font_size == 40

    def test_enumerations_fall_back_when_unknown(self):
        style = SubtitleStyle.from_dict({"position": "middle", "mode": "explode"})
        assert style.position == "bottom"
        assert style.mode == "bottom"

    def test_enumerations_are_case_insensitive(self):
        style = SubtitleStyle.from_dict({"position": "CENTER", "mode": "Karaoke"})
        assert style.position == "center"
        assert style.mode == "karaoke"

    def test_boolean_like_strings_are_understood(self):
        style = SubtitleStyle.from_dict({"bold": "false", "shadow": "no", "uppercase": "1"})
        assert style.bold is False
        assert style.shadow is False
        assert style.uppercase is True


class TestToCss:
    def test_emits_the_documented_keys(self):
        expected = {
            "font-family", "font-size", "font-weight", "line-height", "letter-spacing",
            "color", "-webkit-text-stroke", "paint-order", "text-shadow",
            "text-transform", "max-width", "background-color",
        }
        assert set(SubtitleStyle().to_css()) == expected

    def test_values_are_plain_strings(self):
        for key, value in SubtitleStyle().to_css().items():
            assert isinstance(value, str), f"{key} must be pre-stringified"

    def test_font_size_carries_px(self):
        assert SubtitleStyle(font_size=64).to_css()["font-size"] == "64px"

    def test_max_width_carries_percent(self):
        assert SubtitleStyle(max_width_percent=70).to_css()["max-width"] == "70%"

    def test_bold_and_regular_map_to_distinct_weights(self):
        assert SubtitleStyle(bold=True).to_css()["font-weight"] == "800"
        assert SubtitleStyle(bold=False).to_css()["font-weight"] == "400"

    def test_shadow_off_emits_none(self):
        assert SubtitleStyle(shadow=False).to_css()["text-shadow"] == "none"

    def test_zero_stroke_still_emits_a_valid_declaration(self):
        assert SubtitleStyle(stroke_width=0).to_css()["-webkit-text-stroke"] == "0px #000000"

    def test_zero_opacity_is_transparent(self):
        assert SubtitleStyle(background_opacity=0.0).to_css()["background-color"] == "transparent"

    def test_opacity_becomes_an_rgba_value(self):
        css = SubtitleStyle(background_color="#05070B", background_opacity=0.62).to_css()
        assert css["background-color"] == "rgba(5,7,11,0.62)"

    def test_uppercase_toggles_text_transform(self):
        assert SubtitleStyle(uppercase=True).to_css()["text-transform"] == "uppercase"
        assert SubtitleStyle(uppercase=False).to_css()["text-transform"] == "none"

    @pytest.mark.parametrize(
        "overrides",
        [
            {},
            {"font_size": 120, "stroke_width": 12, "background_opacity": 0.9},
            {"font_family": "Anton", "primary_color": "#22D3EE", "uppercase": True},
        ],
    )
    def test_no_value_contains_a_css_delimiter(self, overrides):
        for key, value in SubtitleStyle.from_dict(overrides).to_css().items():
            for danger in DANGEROUS:
                assert danger not in value, f"{key}={value!r} contains {danger!r}"

    def test_the_block_survives_a_naive_declaration_join(self):
        """Interpolate it the way the renderer does, then prove the result is inert."""
        css = SubtitleStyle().to_css()
        block = ".caption-text {\n    " + ";\n    ".join(
            f"{key}: {value}" for key, value in css.items()
        ) + ";\n}\n"
        # Exactly one rule, one declaration per line, and no stray brace.
        assert block.count("{") == 1 and block.count("}") == 1
        assert block.count(";") == len(css)
        body = block.split("{", 1)[1].rsplit("}", 1)[0]
        for declaration in body.strip().splitlines():
            key, _, value = declaration.strip().rstrip(";").partition(": ")
            assert re.fullmatch(r"-?[a-z-]+", key), f"unsafe key {key!r}"
            assert not re.search(r"[{};]", value), f"unsafe value {value!r}"

    def test_to_css_normalises_rather_than_trusting_the_fields(self):
        css = SubtitleStyle(font_size=999, primary_color="red", font_family="Nope").to_css()
        assert css["font-size"] == "160px"
        assert css["color"] == "#FFFFFF"
        assert css["font-family"] == "Inter"


class TestPresetCatalogue:
    def test_ships_at_least_eight_presets(self):
        assert len(list_presets()) >= 8

    def test_the_named_presets_are_all_present(self):
        required = {
            "cinematic", "bold", "minimal", "karaoke",
            "documentary", "neon", "news", "podcast",
        }
        assert required <= set(preset_names())

    def test_slugs_are_unique_and_url_safe(self):
        names = preset_names()
        assert len(names) == len(set(names))
        for name in names:
            assert re.fullmatch(r"[a-z][a-z0-9_]*", name)

    def test_every_preset_has_a_human_label(self):
        for preset in list_presets():
            assert preset.label and preset.label[0].isupper()

    def test_every_preset_has_at_least_three_valid_palette_colours(self):
        for preset in list_presets():
            assert len(preset.palette) >= 3, f"{preset.name} needs 3+ palette colours"
            for colour in preset.palette:
                assert is_valid_color(colour), f"{preset.name} has invalid {colour!r}"

    def test_every_preset_carries_a_valid_style(self):
        for preset in list_presets():
            style = preset.subtitle.normalised()
            assert is_valid_font_family(style.font_family)
            assert style.position in VALID_POSITIONS
            assert style.mode in VALID_CAPTION_MODES
            assert FONT_SIZE_MIN <= style.font_size <= FONT_SIZE_MAX

    def test_every_preset_uses_a_known_transition_and_effect(self):
        for preset in list_presets():
            assert preset.transition in ("fade", "wipe", "slide_left", "cut")
            assert preset.effect in ("cinematic", "glow", "clean")

    def test_preset_styles_survive_the_css_boundary(self):
        for preset in list_presets():
            for key, value in preset.subtitle.to_css().items():
                for danger in DANGEROUS:
                    assert danger not in value, f"{preset.name}/{key} leaked {danger!r}"

    def test_the_listed_presets_are_detached_copies(self):
        first = list_presets()[0]
        first.subtitle.font_size = 160
        first.palette.append("#FFFFFF")
        assert list_presets()[0].subtitle.font_size == 52
        assert "#FFFFFF" not in list_presets()[0].palette

    def test_cinematic_matches_the_defaults(self):
        preset = get_preset("cinematic")
        assert preset.name == "cinematic"
        assert preset.subtitle == SubtitleStyle()

    def test_bold_is_an_uppercase_anton_hook(self):
        preset = get_preset("bold")
        assert preset.subtitle.font_family == "Anton"
        assert preset.subtitle.uppercase is True
        assert preset.subtitle.stroke_width >= 6
        assert preset.subtitle.mode == "hook"

    def test_minimal_is_small_unstroked_and_low(self):
        preset = get_preset("minimal")
        assert preset.subtitle.stroke_width == 0
        assert preset.subtitle.shadow is False
        assert preset.subtitle.position == "lower"
        assert preset.subtitle.font_size < 45

    def test_karaoke_is_boxed_and_centred(self):
        preset = get_preset("karaoke")
        assert preset.subtitle.mode == "karaoke"
        assert preset.subtitle.position == "center"
        assert 0.5 <= preset.subtitle.background_opacity <= 0.75

    def test_documentary_is_a_serif_at_the_bottom(self):
        preset = get_preset("documentary")
        assert preset.subtitle.font_family == "Playfair Display"
        assert preset.subtitle.position == "bottom"
        assert 0 < preset.subtitle.stroke_width <= 3

    def test_neon_is_cyan_on_a_glow(self):
        preset = get_preset("neon")
        assert preset.subtitle.primary_color == "#22D3EE"
        assert preset.effect == "glow"

    def test_news_is_clean_inter_at_the_top(self):
        preset = get_preset("news")
        assert preset.subtitle.font_family == "Inter"
        assert preset.subtitle.shadow is False
        assert preset.subtitle.position == "upper"

    def test_podcast_is_large_and_boxed(self):
        preset = get_preset("podcast")
        assert preset.subtitle.font_size >= 55
        assert preset.subtitle.background_opacity > 0.5
        assert preset.subtitle.position == "bottom"

    def test_preset_to_dict_is_json_ready(self):
        import json

        payload = get_preset("neon").to_dict()
        assert set(payload) == {
            "name", "label", "subtitle", "palette", "transition", "effect",
        }
        assert json.loads(json.dumps(payload)) == payload

    def test_preset_to_dict_palette_is_a_copy(self):
        preset = get_preset("neon")
        payload = preset.to_dict()
        payload["palette"].append("#FFFFFF")
        assert len(preset.palette) == 3


class TestGetPreset:
    def test_unknown_name_returns_cinematic(self):
        assert get_preset("nonexistent").name == "cinematic"

    @pytest.mark.parametrize("name", [None, 42, "", "   ", [], {"name": "neon"}])
    def test_non_string_and_blank_names_never_raise(self, name):
        assert get_preset(name).name == "cinematic"

    def test_lookup_is_case_and_whitespace_insensitive(self):
        assert get_preset("  NEON ").name == "neon"

    def test_injection_attempts_fall_back(self):
        assert get_preset("neon; }body{display:none").name == "cinematic"

    def test_returned_preset_is_a_copy(self):
        preset = get_preset("bold")
        preset.subtitle.font_size = 160
        assert get_preset("bold").subtitle.font_size == 78


class TestResolvePresetAndStyle:
    def test_merges_overrides_onto_the_named_preset(self):
        preset, style = resolve_preset_and_style("neon", {"font_size": 40})
        assert preset.name == "neon"
        assert style.font_size == 40
        # Fields the caller did not mention keep the preset's values.
        assert style.font_family == "Bebas Neue"
        assert style.primary_color == "#22D3EE"
        assert preset.subtitle == style

    def test_the_returned_preset_reflects_the_final_style(self):
        preset, style = resolve_preset_and_style("cinematic", {"primary_color": "#22D3EE"})
        assert preset.subtitle.primary_color == "#22D3EE"
        assert preset.subtitle == style
        assert preset.palette == get_preset("cinematic").palette

    def test_overrides_are_re_normalised(self):
        _, style = resolve_preset_and_style("cinematic", {"font_size": 9999})
        assert style.font_size == FONT_SIZE_MAX

    def test_an_invalid_override_falls_back_to_the_preset_value(self):
        _, style = resolve_preset_and_style("karaoke", {"primary_color": "chartreuse"})
        assert style.primary_color == "#FFFFFF"
        assert style.background_opacity == pytest.approx(0.62)

    def test_an_unknown_preset_name_still_resolves(self):
        preset, style = resolve_preset_and_style("nonexistent", {"font_size": 33})
        assert preset.name == "cinematic"
        assert style.font_size == 33

    @pytest.mark.parametrize("name", [None, "", 7, ["cinematic"], "neon; }"])
    def test_bad_preset_names_use_the_default(self, name):
        preset, _ = resolve_preset_and_style(name, None)
        assert preset.name == "cinematic"

    @pytest.mark.parametrize("overrides", [None, {}, [], "font_size=40", 5])
    def test_bad_overrides_leave_the_preset_untouched(self, overrides):
        preset, style = resolve_preset_and_style("bold", overrides)
        assert preset.name == "bold"
        assert style == get_preset("bold").subtitle

    def test_resolution_never_mutates_the_catalogue(self):
        resolve_preset_and_style("neon", {"font_size": 20})
        assert get_preset("neon").subtitle.font_size == 68

    @pytest.mark.parametrize("hostile", HOSTILE_INPUTS)
    def test_every_hostile_payload_still_yields_a_renderable_style(self, hostile):
        for name in ("cinematic", "bold", None, "nonexistent"):
            preset, style = resolve_preset_and_style(name, hostile)
            assert isinstance(preset, ThemePreset)
            assert style == style.normalised()
            assert is_valid_font_family(style.font_family)
            assert style.position in VALID_POSITIONS


class TestPositionMapping:
    @pytest.mark.parametrize("position", VALID_POSITIONS)
    def test_every_position_maps_to_a_container_alignment(self, position):
        value = align_for_position(position)
        assert value in ("flex-start", "center", "flex-end")

    def test_top_and_bottom_pull_to_opposite_edges(self):
        assert align_for_position("top") != align_for_position("bottom")

    def test_an_unknown_position_falls_back_to_the_bottom_alignment(self):
        assert align_for_position("sideways") == align_for_position("bottom")
