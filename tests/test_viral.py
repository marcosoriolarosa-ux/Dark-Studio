"""Tests for viral pipeline: beat detection, loop, thumbnail, platform metadata."""

import pytest
from pathlib import Path

from backend.services.viral_pipeline import (
    detect_beats,
    snap_to_nearest_beat,
    build_beat_synced_storyboard,
    make_seamless_loop,
    generate_optimized_thumbnail,
    build_platform_metadata,
)


class TestBeatDetection:
    def test_detect_beats_returns_structured_data(self):
        # Use a known test audio file
        audio_path = Path("storage/uploads/media_test.mp3")
        if not audio_path.exists():
            pytest.skip("Test audio not available")
        result = detect_beats(audio_path)
        assert "tempo" in result
        assert "beats" in result
        assert "beat_count" in result
        assert "duration" in result
        assert isinstance(result["beats"], list)
        assert result["beat_count"] == len(result["beats"])
        assert result["duration"] > 0

    def test_snap_to_nearest_beat_within_tolerance(self):
        beats = [1.0, 2.0, 3.0, 4.0, 5.0]
        # Within tolerance
        assert snap_to_nearest_beat(1.05, beats) == 1.0
        assert snap_to_nearest_beat(2.95, beats) == 3.0
        # Outside tolerance returns original
        assert snap_to_nearest_beat(1.5, beats) == 1.5
        # Empty beats returns original
        assert snap_to_nearest_beat(2.5, []) == 2.5

    def test_build_beat_synced_storyboard_snaps_to_beats(self):
        segments = [
            {"index": 1, "start": 0.0, "end": 2.5, "text": "Scene one"},
            {"index": 2, "start": 2.5, "end": 5.5, "text": "Scene two"},
            {"index": 3, "start": 5.5, "end": 8.0, "text": "Scene three"},
        ]
        beats = [0.0, 2.0, 4.0, 6.0, 8.0]
        storyboard = build_beat_synced_storyboard(segments, beats)
        assert len(storyboard) == 3
        # First scene should be hooked to <= 3s
        assert storyboard[0]["duration"] <= 3.0
        # Source times (before overlap normalization) should be snapped to beats
        for scene in storyboard:
            src_start = scene.get("source_start", scene["start"])
            src_end = scene.get("source_end", scene["end"])
            assert any(abs(src_start - b) < 0.2 for b in beats) or src_start == 0.0
            assert any(abs(src_end - b) < 0.2 for b in beats)
        # Overlap is applied after snapping - verify overlap exists
        assert storyboard[1]["start"] < storyboard[0]["end"]
        assert storyboard[2]["start"] < storyboard[1]["end"]

    def test_first_scene_hook_duration(self):
        segments = [
            {"index": 1, "start": 0.0, "end": 10.0, "text": "Very long first scene"},
        ]
        beats = [0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
        storyboard = build_beat_synced_storyboard(segments, beats)
        assert storyboard[0]["duration"] <= 3.0


class TestSeamlessLoop:
    def test_make_seamless_loop_adds_loop_metadata(self):
        storyboard = [
            {"index": 1, "start": 0.0, "end": 3.0, "duration": 3.0, "media_url": "url1", "caption": "Hook"},
            {"index": 2, "start": 3.0, "end": 6.0, "duration": 3.0, "media_url": "url2", "caption": "Body"},
        ]
        result = make_seamless_loop(storyboard)
        assert "loop_head" in result[0]
        assert "loop_tail" in result[-1]
        assert result[0]["loop_head"]["duration"] == 0.5
        assert result[-1]["loop_tail"]["duration"] == 0.5
        assert result[0]["loop_head"]["visual"] == "url1"
        assert result[-1]["loop_tail"]["visual"] == "url2"


class TestThumbnailGeneration:
    def test_generate_optimized_thumbnail_creates_file(self, tmp_path):
        storyboard = [
            {"index": 1, "media_url": "https://example.com/image.jpg", "caption": "Hook text here", "start": 0, "end": 3},
        ]
        thumb_path = generate_optimized_thumbnail(storyboard, "test_project", 1080, 1920)
        assert thumb_path.endswith("_thumb.html")
        assert Path(thumb_path).exists()
        content = Path(thumb_path).read_text(encoding="utf-8")
        assert "HOOK TEXT HERE" in content.upper()
        assert "brightness(0.55)" in content
        assert "96px" in content

    def test_thumbnail_handles_missing_media(self, tmp_path):
        storyboard = [{"index": 1, "media_url": "", "caption": "No media", "start": 0, "end": 3}]
        thumb_path = generate_optimized_thumbnail(storyboard, "test_nomedia", 1080, 1920)
        assert Path(thumb_path).exists()
        content = Path(thumb_path).read_text(encoding="utf-8")
        assert "NO MEDIA" in content.upper()


class TestPlatformMetadata:
    def test_build_platform_metadata_has_required_fields(self):
        storyboard = [
            {"caption": "Dinheiro é controle de tempo humano"},
            {"caption": "Você trabalha 2000 horas por ano"},
            {"caption": "Entenda a engrenagem e construa riqueza"},
        ]
        meta = build_platform_metadata(storyboard, topic="dinheiro", niche="finance")
        assert "hashtags" in meta
        assert "category" in meta
        assert "title_options" in meta
        assert "description" in meta
        assert "sound_suggestions" in meta
        assert "posting_times" in meta
        assert len(meta["hashtags"]) >= 5
        assert meta["category"] == "Education"
        assert len(meta["title_options"]) == 3

    def test_hashtags_from_caption_keywords(self):
        storyboard = [
            {"caption": "Bitcoin blockchain criptomoeda investimento"},
            {"caption": "Ethereum smart contracts defi"},
        ]
        meta = build_platform_metadata(storyboard, topic="crypto", niche="finance")
        tags = meta["hashtags"]
        # Should extract meaningful terms
        assert any("bitcoin" in tag.lower() or "cripto" in tag.lower() or "invest" in tag.lower() for tag in tags)


class TestViralPipelineIntegration:
    @pytest.mark.asyncio
    async def test_render_viral_video_full_pipeline(self):
        """Integration test - requires audio and SRT files."""
        from backend.services.viral_pipeline import render_viral_video
        srt_path = Path("storage/uploads/media_test.srt")
        if not srt_path.exists():
            pytest.skip("Test media not available")
        result = await render_viral_video(
            project_name="media_test",
            srt_path=srt_path,
            aspect_ratio="vertical",
            include_karaoke=True,
            topic="dinheiro",
            niche="finance",
        )
        assert result.get("status") == "rendered"
        assert result.get("output_path", "").endswith("_viral.mp4")
        assert result.get("video_duration", 0) > 0
        assert "viral_meta" in result
        assert "beat_data" in result["viral_meta"]
        assert "platform_metadata" in result["viral_meta"]
        assert "thumbnail_path" in result["viral_meta"]
        # Verify meta file was saved
        meta_file = Path("storage/outputs/media_test_viral_meta.json")
        assert meta_file.exists()
        import json
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        assert meta["beat_data"]["beat_count"] > 0
        assert meta["platform_metadata"]["category"] == "Education"