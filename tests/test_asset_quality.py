"""Tests for padded-asset detection.

Stock providers sometimes return an image wrapped in a large uniform white canvas.
object-fit: cover reproduces that canvas faithfully, so the frame looks broken even
though the renderer behaved correctly. Detection uses ffmpeg signalstats, so these
tests mock the measurement layer and keep the thresholds under test.
"""
from pathlib import Path
from unittest import mock

import pytest

from backend.services import asset_quality as aq


PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000a49444154789c6360000002000100ffff0300000600"
    "0557bfabd40000000049454e44ae426082"
)


class TestImageDimensions:
    def test_parses_comma_separated_probe_output(self, tmp_path):
        image = tmp_path / "a.png"
        image.write_bytes(PNG_BYTES)
        with mock.patch.object(aq.subprocess, "run") as run:
            run.return_value = mock.Mock(stdout="1880,1246\n", stderr="")
            assert aq.image_dimensions(image) == (1880, 1246)

    def test_parses_x_separated_probe_output(self, tmp_path):
        image = tmp_path / "a.png"
        image.write_bytes(PNG_BYTES)
        with mock.patch.object(aq.subprocess, "run") as run:
            run.return_value = mock.Mock(stdout="1880x1246\n", stderr="")
            assert aq.image_dimensions(image) == (1880, 1246)

    def test_returns_none_when_probe_fails(self, tmp_path):
        image = tmp_path / "a.png"
        image.write_bytes(PNG_BYTES)
        with mock.patch.object(aq.subprocess, "run") as run:
            run.return_value = mock.Mock(stdout="", stderr="boom")
            assert aq.image_dimensions(image) is None


class TestUniformBrightHelper:
    def test_flat_and_bright_counts_as_padding_like(self):
        assert aq._is_uniform_bright({"yavg": 255.0, "ymin": 254.0, "ymax": 255.0}) is True

    def test_bright_but_detailed_is_content(self):
        assert aq._is_uniform_bright({"yavg": 240.0, "ymin": 10.0, "ymax": 255.0}) is False

    def test_dark_is_not_padding(self):
        assert aq._is_uniform_bright({"yavg": 40.0, "ymin": 38.0, "ymax": 42.0}) is False

    def test_incomplete_stats_are_not_padding(self):
        assert aq._is_uniform_bright({"yavg": 255.0}) is False


class TestLooksPadded:
    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        aq._VERDICT_CACHE.clear()

    def _image(self, tmp_path):
        image = tmp_path / "scene.jpg"
        image.write_bytes(b"\xff\xd8\xff" + b"x" * 4096)
        return image

    def test_flat_bright_border_on_a_darker_frame_is_padded(self, tmp_path):
        image = self._image(tmp_path)
        calls = {"n": 0}

        def fake_stats(_image_path, crop):
            calls["n"] += 1
            if crop is None:
                return {"yavg": 178.0, "ymin": 20.0, "ymax": 255.0}
            return {"yavg": 255.0, "ymin": 254.0, "ymax": 255.0}

        with mock.patch.object(aq, "image_dimensions", return_value=(1880, 1246)), \
             mock.patch.object(aq, "_run_stats", side_effect=fake_stats):
            assert aq.looks_padded(image) is True

    def test_an_entirely_bright_photo_is_not_padded(self, tmp_path):
        """A high-key photo is not a padded canvas; rejecting it would be wrong."""
        image = self._image(tmp_path)

        def fake_stats(_image_path, crop):
            if crop is None:
                return {"yavg": 250.0, "ymin": 240.0, "ymax": 255.0}
            return {"yavg": 252.0, "ymin": 248.0, "ymax": 255.0}

        with mock.patch.object(aq, "image_dimensions", return_value=(1200, 800)), \
             mock.patch.object(aq, "_run_stats", side_effect=fake_stats):
            assert aq.looks_padded(image) is False

    def test_shallow_edge_but_detailed_deep_is_content(self, tmp_path):
        """Depth matters: a flat edge that gains detail further in is a vignette."""
        image = self._image(tmp_path)
        flat = {"yavg": 250.0, "ymin": 246.0, "ymax": 255.0}
        detailed = {"yavg": 120.0, "ymin": 10.0, "ymax": 230.0}
        dark = {"yavg": 100.0, "ymin": 5.0, "ymax": 240.0}
        short_side = 800
        shallow_px = int(short_side * aq._SHALLOW)

        def fake_stats(_image_path, crop):
            if crop is None:
                return dark
            parts = [int(value) for value in crop.split(":")]
            thickness = min(parts[0], parts[1])  # a strip's thin dimension
            return flat if thickness <= shallow_px + 1 else detailed

        with mock.patch.object(aq, "image_dimensions", return_value=(1200, short_side)), \
             mock.patch.object(aq, "_run_stats", side_effect=fake_stats):
            assert aq.looks_padded(image) is False

    def test_flat_all_the_way_in_is_padded(self, tmp_path):
        image = self._image(tmp_path)
        flat = {"yavg": 250.0, "ymin": 246.0, "ymax": 255.0}
        dark = {"yavg": 100.0, "ymin": 5.0, "ymax": 240.0}

        with mock.patch.object(aq, "image_dimensions", return_value=(1200, 800)), \
             mock.patch.object(aq, "_run_stats", side_effect=lambda _i, crop: dark if crop is None else flat):
            assert aq.looks_padded(image) is True

    def test_dimensions_unknown_means_not_padded(self, tmp_path):
        image = self._image(tmp_path)
        with mock.patch.object(aq, "image_dimensions", return_value=None):
            assert aq.looks_padded(image) is False

    def test_missing_file_is_not_padded(self, tmp_path):
        assert aq.looks_padded(tmp_path / "ausente.jpg") is False

    def test_video_is_skipped(self, tmp_path):
        clip = tmp_path / "scene.mp4"
        clip.write_bytes(b"\x00" * 2048)
        assert aq.looks_padded(clip) is False

    def test_verdict_is_cached_per_content(self, tmp_path):
        image = self._image(tmp_path)
        with mock.patch.object(aq, "image_dimensions", return_value=(1000, 1000)), \
             mock.patch.object(aq, "_run_stats", return_value={"yavg": 10.0}) as stats:
            aq.looks_padded(image)
            first = stats.call_count
            aq.looks_padded(image)
            assert stats.call_count == first, "the verdict must be cached by content hash"

    def test_identical_content_at_a_second_path_hits_the_cache(self, tmp_path):
        first = self._image(tmp_path)
        second = tmp_path / "outro.jpg"
        second.write_bytes(first.read_bytes())
        assert first.read_bytes() == second.read_bytes()
        with mock.patch.object(aq, "image_dimensions", return_value=(1000, 1000)) as dims, \
             mock.patch.object(aq, "_run_stats", return_value={"yavg": 10.0, "ymin": 8.0, "ymax": 12.0}):
            aq.looks_padded(first)
            aq.looks_padded(second)
            assert dims.call_count == 1, "identical bytes must reuse the cached verdict"


class TestDescribe:
    def test_reports_dimensions_and_verdict(self, tmp_path):
        image = tmp_path / "a.jpg"
        image.write_bytes(b"\xff\xd8\xff" + b"y" * 4096)
        with mock.patch.object(aq, "image_dimensions", return_value=(800, 600)), \
             mock.patch.object(aq, "_run_stats", return_value={"yavg": 64.0}), \
             mock.patch.object(aq, "looks_padded", return_value=True):
            info = aq.describe(image)
        assert info["width"] == 800
        assert info["height"] == 600
        assert info["padded"] is True
        assert info["full_luma"] == 64.0