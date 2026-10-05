"""API tests for the wave 1 (script / TTS / style / music) and wave 2 wiring.

No network: every service call that would reach a provider is mocked on
``backend.app`` itself, because the module imports the service names into its own
namespace (that is the only seam ``mock.patch.object(app_module, ...)`` can reach).

The ``librosa`` and CORS-environment checks run in a subprocess: an import blocker
and an env var both have to be in place *before* ``backend.app`` is imported, and
reloading the module in-process would leak a second app into the other suites.
"""
import dataclasses
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from backend import app as app_module
from backend.services import music as music_service
from backend.services import script_gen
from backend.services.auth_contract import AUTH_MISSING_KEY, AuthError
from backend.services.script_gen import Script, ScriptSection
from backend.services.tts import DEFAULT_VOICE

REPO_ROOT = Path(__file__).resolve().parents[1]

client = TestClient(app_module.app)

# Every key tests/test_e2e.py and tests/test_visual_terms.py already read.
BUILD_VIDEO_KEYS = {
    "project_name", "keywords", "media", "scene_media", "segments", "storyboard",
    "edit_plan", "scene_count", "scenes_with_media", "term_source", "provider_status",
    "render", "render_real",
}

SEGMENTS = [
    {"index": 1, "start": 0.0, "end": 2.5, "text": "Primeira frase."},
    {"index": 2, "start": 2.5, "end": 5.0, "text": "Segunda frase."},
]

SRT_TEXT = (
    "1\n00:00:00,000 --> 00:00:02,500\nPrimeira frase.\n\n"
    "2\n00:00:02,500 --> 00:00:05,000\nSegunda frase.\n"
)


def _script(source: str = "openrouter-free", fallback_error=None) -> Script:
    return Script(
        title="O custo invisível",
        hook="Quase ninguém repara.",
        sections=[
            ScriptSection(index=1, text="A primeira parte do guião.", visual_terms=["banknotes"]),
            ScriptSection(index=2, text="A segunda parte do guião.", visual_terms=["coins"]),
        ],
        language="pt-PT",
        tone="documentary",
        source=source,
        model=None if fallback_error else "some/model:free",
        fallback_error=fallback_error,
    )


async def _fake_tts(text, output_path, **kwargs):
    """Writes a placeholder MP3; the real service is what talks to a provider."""
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    Path(output_path).write_bytes(b"ID3\x00\x00\x00\x00fake-mp3-bytes")
    return Path(output_path)


@pytest.fixture
def storage(tmp_path, monkeypatch):
    """Point the app at a throwaway uploads directory and an empty music library.

    The empty music directory is the fresh-checkout state: storage/music/ is
    gitignored, and the endpoints now heal it. The builtin specs are shortened to
    1s here for the same reason tests/test_music.py shortens them, so that the
    real synthesis stays a fixture detail instead of seconds of suite time.
    """
    uploads = tmp_path / "uploads"
    music_dir = tmp_path / "music"
    uploads.mkdir()
    music_dir.mkdir()
    monkeypatch.setattr(app_module, "UPLOAD_DIR", uploads)
    monkeypatch.setattr(app_module, "MUSIC_DIR", music_dir)
    # register_upload resolves MUSIC_DIR from its own module, so both must move.
    monkeypatch.setattr(music_service, "MUSIC_DIR", music_dir)
    monkeypatch.setattr(
        music_service,
        "BUILTIN_SPECS",
        tuple(
            dataclasses.replace(spec, duration=1.0)
            for spec in music_service.BUILTIN_SPECS
        ),
    )
    monkeypatch.setattr(
        music_service,
        "_BUILTIN_BY_ID",
        dict((spec.id, spec) for spec in music_service.BUILTIN_SPECS),
    )
    return uploads


def _subprocess(code: str, env_extra: dict = None) -> str:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT)
    env.pop("DARK_STUDIO_ALLOWED_ORIGINS", None)
    env.update(env_extra or {})
    proc = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc.stdout


# --------------------------------------------------------------- GET endpoints
class TestNewGetEndpoints:
    def test_languages_lists_codes_and_labels(self):
        response = client.get("/api/languages")
        assert response.status_code == 200
        body = response.json()
        assert body["languages"]
        for item in body["languages"]:
            assert set(item) >= {"code", "label"}
        assert "pt-PT" in {item["code"] for item in body["languages"]}

    def test_presets_returns_catalogue_and_vocabularies(self):
        response = client.get("/api/presets")
        assert response.status_code == 200
        body = response.json()
        assert body["presets"]
        for preset in body["presets"]:
            assert set(preset) >= {"name", "label", "subtitle", "palette"}
            assert set(preset["subtitle"]) >= {"font_family", "font_size", "position", "mode"}
        assert "bottom" in body["positions"]
        assert "karaoke" in body["modes"]
        assert "Inter" in body["fonts"]

    def test_tts_status_reports_providers(self):
        response = client.get("/api/tts/status")
        assert response.status_code == 200
        body = response.json()
        assert set(body["providers"]) >= {"edge", "openai", "azure"}
        assert body["default_voice"] == DEFAULT_VOICE

    def test_music_tracks_shape(self, storage):
        """A fresh checkout ships an empty storage/music/; the endpoint heals it."""
        assert list(music_service.MUSIC_DIR.iterdir()) == [], (
            "the fixture must be the empty state of a fresh checkout"
        )
        response = client.get("/api/music/tracks")
        assert response.status_code == 200
        body = response.json()
        tracks = body["tracks"]
        assert tracks, "the picker must not be empty on a fresh checkout"
        required = set("id title mood duration path builtin source".split())
        for item in tracks:
            assert required <= set(item)
        assert all(item["builtin"] for item in tracks)
        assert body["mood"] == ""
        assert "ambient" in body["moods"]
        # The heal is real work on disk, not a response body assembled in memory.
        assert len(list(music_service.MUSIC_DIR.glob("*.wav"))) == len(tracks)

    def test_music_tracks_filters_by_mood_after_healing(self, storage):
        body = client.get("/api/music/tracks", params={"mood": "dark"}).json()
        assert body["mood"] == "dark"
        assert [item["id"] for item in body["tracks"]] == ["dark-depths"]

    def test_music_tracks_is_empty_when_the_library_cannot_be_built(self, storage):
        # The self-heal can still fail (no numpy, no writable directory). The
        # endpoint then answers 200 with an empty list rather than invent tracks.
        with mock.patch.object(app_module, "ensure_builtin_library", return_value=[]):
            body = client.get("/api/music/tracks").json()
        assert body["tracks"] == []
        assert "ambient" in body["moods"]

    def test_music_tracks_rejects_an_unknown_mood(self, storage):
        response = client.get("/api/music/tracks", params={"mood": "screaming"})
        assert response.status_code == 400
        assert response.json()["detail"]

    def test_music_search_requires_a_query(self, storage):
        assert client.get("/api/music/search", params={"q": "  "}).status_code == 400
        assert client.get("/api/music/search", params={"q": ""}).status_code == 400

    def test_music_search_sees_the_library_it_heals(self, storage):
        # Same defect as the tracks endpoint: a search of an unmaterialised
        # library finds nothing, so the picker reads as broken.
        assert list(music_service.MUSIC_DIR.iterdir()) == [], (
            "the fixture must be the empty state of a fresh checkout"
        )
        response = client.get("/api/music/search", params={"q": "ambient"})
        assert response.status_code == 200
        body = response.json()
        assert body["query"] == "ambient"
        assert [item["id"] for item in body["tracks"]] == ["ambient-drift"]

    def test_music_search_is_empty_when_the_library_cannot_be_built(self, storage):
        with mock.patch.object(app_module, "ensure_builtin_library", return_value=[]):
            body = client.get("/api/music/search", params={"q": "ambient"}).json()
        assert body["tracks"] == []


class TestVoices:
    def test_returns_the_full_catalogue_and_the_default(self):
        response = client.get("/api/voices")
        assert response.status_code == 200
        body = response.json()
        assert body["default"] == DEFAULT_VOICE
        assert len(body["voices"]) > 1
        assert set(body["voices"][0]) >= {"id", "name", "gender", "locale", "provider"}

    def test_filters_by_locale_prefix(self):
        body = client.get("/api/voices", params={"locale": "pt-PT"}).json()
        assert body["voices"]
        assert {voice["locale"] for voice in body["voices"]} == {"pt-PT"}

    def test_bare_language_returns_every_variant(self):
        body = client.get("/api/voices", params={"locale": "pt"}).json()
        assert {"pt-PT", "pt-BR"} <= {voice["locale"] for voice in body["voices"]}

    def test_unmatched_locale_is_an_empty_list_not_an_error(self):
        response = client.get("/api/voices", params={"locale": "xx-YY"})
        assert response.status_code == 200
        assert response.json()["voices"] == []


# ------------------------------------------------------------ POST /api/script
class TestScript:
    def test_model_script_is_returned_in_full(self):
        with mock.patch.object(app_module, "generate_script", return_value=_script()) as gen:
            response = client.post("/api/script", json={"topic": "dinheiro", "section_count": 2})
        assert response.status_code == 200
        body = response.json()
        assert set(body) >= {
            "title", "hook", "sections", "language", "tone", "source", "model",
            "fallback_error", "full_text", "estimated_seconds", "section_count", "degraded",
        }
        assert body["source"] == "openrouter-free"
        assert body["degraded"] is False
        assert body["section_count"] == 2
        assert body["sections"][0]["visual_terms"] == ["banknotes"]
        # Synchronous service, keyword-only arguments.
        _, kwargs = gen.call_args
        assert kwargs["language"] == "pt-PT"
        assert kwargs["section_count"] == 2
        assert kwargs["duration_target"] == 60

    def test_fallback_script_is_a_200_and_says_so(self):
        fallback = _script(source="fallback-local", fallback_error="sem chave OpenRouter")
        with mock.patch.object(app_module, "generate_script", return_value=fallback):
            response = client.post("/api/script", json={"topic": "dinheiro"})
        assert response.status_code == 200
        body = response.json()
        assert body["source"] == "fallback-local"
        assert body["degraded"] is True
        assert "sem chave" in body["fallback_error"]
        assert body["full_text"]

    def test_stores_the_script_for_a_named_project(self, storage):
        with mock.patch.object(app_module, "generate_script", return_value=_script()):
            response = client.post(
                "/api/script", json={"topic": "dinheiro", "project_name": "meu projeto"}
            )
        assert response.status_code == 200
        assert response.json()["project_name"] == "meuprojeto"
        assert (storage / "meuprojeto.script.json").exists()

    def test_blank_topic_is_a_400_and_never_calls_the_service(self):
        with mock.patch.object(app_module, "generate_script") as gen:
            response = client.post("/api/script", json={"topic": "   "})
        assert response.status_code == 400
        assert response.json()["detail"]
        gen.assert_not_called()

    @pytest.mark.parametrize("section_count", [0, -1, script_gen.MAX_SECTIONS + 1])
    def test_out_of_range_section_count_is_a_400(self, section_count):
        with mock.patch.object(app_module, "generate_script") as gen:
            response = client.post(
                "/api/script", json={"topic": "dinheiro", "section_count": section_count}
            )
        assert response.status_code == 400
        gen.assert_not_called()

    def test_unknown_language_is_a_400(self):
        response = client.post("/api/script", json={"topic": "x", "language": "kl-KL"})
        assert response.status_code == 400

    def test_value_error_from_the_service_is_a_400(self):
        with mock.patch.object(
            app_module, "generate_script", side_effect=ValueError("Indique um tema.")
        ):
            response = client.post("/api/script", json={"topic": "x"})
        assert response.status_code == 400
        assert "tema" in response.json()["detail"]

    def test_unexpected_service_failure_is_a_422_not_a_500(self):
        with mock.patch.object(
            app_module, "generate_script", side_effect=MemoryError("boom")
        ):
            response = client.post("/api/script", json={"topic": "x"})
        assert response.status_code == 422


# -------------------------------------------------------------- POST /api/tts
class TestTts:
    def _tts_patches(self, side_effect=_fake_tts):
        synth = mock.patch.object(
            app_module, "synthesize_speech_long",
            mock.AsyncMock(side_effect=side_effect),
        )
        transcribe = mock.patch.object(
            app_module, "transcribe_audio_file", return_value=SEGMENTS
        )
        return synth, transcribe

    def test_happy_path_writes_audio_and_srt(self, storage):
        synth, transcribe = self._tts_patches()
        with synth, transcribe:
            response = client.post(
                "/api/tts", json={"project_name": "meu projeto", "text": "Olá mundo"}
            )
        assert response.status_code == 200
        body = response.json()
        assert set(body) == {
            "project_name", "audio_file", "srt_file", "segments", "voice",
            "provider", "characters", "estimated_seconds",
        }
        assert body["project_name"] == "meuprojeto"
        assert body["segments"] == SEGMENTS
        assert body["voice"] == DEFAULT_VOICE
        assert body["provider"] == "edge"
        assert body["characters"] == len("Olá mundo")
        assert body["estimated_seconds"] > 0
        assert (storage / "meuprojeto.mp3").exists()
        assert (storage / "meuprojeto.srt").read_text(encoding="utf-8") == SRT_TEXT

    def test_forwards_voice_and_prosody_to_the_service(self, storage):
        synth, transcribe = self._tts_patches()
        with synth, transcribe:
            response = client.post("/api/tts", json={
                "project_name": "p", "text": "Olá",
                "voice": "en-US-JennyNeural", "provider": "azure",
                "rate": "+10%", "volume": "-5%", "pitch": "+2Hz",
            })
        assert response.status_code == 200
        # The default Edge voice is auto-mapped by the service for keyed providers;
        # an explicit voice must survive untouched.
        _, kwargs = synth.new.await_args
        assert kwargs["voice"] == "en-US-JennyNeural"
        assert kwargs["provider"] == "azure"
        assert kwargs["rate"] == "+10%"
        assert kwargs["volume"] == "-5%"
        assert kwargs["pitch"] == "+2Hz"

    def test_empty_text_without_a_stored_script_is_a_400(self, storage):
        synth, transcribe = self._tts_patches()
        with synth, transcribe as transcribe_mock:
            response = client.post("/api/tts", json={"project_name": "vazio"})
        assert response.status_code == 400
        assert response.json()["detail"]
        transcribe_mock.assert_not_called()

    def test_empty_text_narrates_the_stored_script(self, storage):
        with mock.patch.object(app_module, "generate_script", return_value=_script()):
            stored = client.post(
                "/api/script", json={"topic": "dinheiro", "project_name": "guardado"}
            )
        assert stored.status_code == 200
        synth, transcribe = self._tts_patches()
        with synth, transcribe:
            response = client.post("/api/tts", json={"project_name": "guardado"})
        assert response.status_code == 200
        text = synth.new.await_args[0][0]
        assert "primeira parte do guião" in text

    def test_invalid_voice_id_is_a_400(self, storage):
        synth, transcribe = self._tts_patches(
            side_effect=ValueError("Unknown Edge voice id: 'nao-existe'")
        )
        with synth, transcribe:
            response = client.post(
                "/api/tts", json={"project_name": "p", "text": "Olá", "voice": "nao-existe"}
            )
        assert response.status_code == 400
        assert "nao-existe" in response.json()["detail"]

    def test_unknown_provider_is_a_400(self, storage):
        response = client.post(
            "/api/tts", json={"project_name": "p", "text": "Olá", "provider": "hal9000"}
        )
        assert response.status_code == 400

    def test_missing_edge_tts_package_is_a_422(self, storage):
        synth, transcribe = self._tts_patches(
            side_effect=RuntimeError("the optional package 'edge-tts' is not installed")
        )
        with synth, transcribe:
            response = client.post("/api/tts", json={"project_name": "p", "text": "Olá"})
        assert response.status_code == 422
        assert "edge-tts" in response.json()["detail"]

    def test_auth_error_surfaces_its_status_and_code(self, storage):
        error = AuthError(
            AUTH_MISSING_KEY,
            "No API key configured for provider 'azure'.",
            401,
            {"provider": "azure"},
        )
        synth, transcribe = self._tts_patches(side_effect=error)
        with synth, transcribe:
            response = client.post("/api/tts", json={
                "project_name": "p", "text": "Olá", "provider": "azure",
            })
        assert response.status_code == 401
        body = response.json()
        assert body["error_code"] == AUTH_MISSING_KEY
        assert body["details"]["provider"] == "azure"

    def test_subtitle_failure_is_a_422(self, storage):
        synth = mock.patch.object(
            app_module, "synthesize_speech_long",
            mock.AsyncMock(side_effect=_fake_tts),
        )
        transcribe = mock.patch.object(
            app_module, "transcribe_audio_file", side_effect=RuntimeError("ffmpeg exploded")
        )
        with synth, transcribe:
            response = client.post("/api/tts", json={"project_name": "p", "text": "Olá"})
        assert response.status_code == 422
        assert "ffmpeg" in response.json()["detail"]


# ---------------------------------------------------------- /api/music/upload
class TestMusicUpload:
    def _post(self, filename: str, content: bytes = b"ID3fake", title: str = "", mood: str = ""):
        data = {}
        if title:
            data["title"] = title
        if mood:
            data["mood"] = mood
        return client.post(
            "/api/music/upload",
            files={"file": (filename, content, "audio/mpeg")},
            data=data,
        )

    def test_rejects_an_executable(self, storage):
        response = self._post("musica.exe")
        assert response.status_code == 400
        assert response.json()["detail"]

    def test_rejects_a_file_without_an_extension(self, storage):
        assert self._post("musica").status_code == 400

    def test_accepts_an_mp3_and_returns_the_track(self, storage):
        response = self._post("minha musica.mp3", title="Minua Musica", mood="lofi")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        track = body["track"]
        assert set(track) >= {"id", "title", "mood", "duration", "path", "builtin", "source"}
        assert track["source"] == "upload"
        assert track["mood"] == "lofi"
        assert Path(track["path"]).suffix == ".mp3"
        assert Path(track["path"]).exists()

    def test_title_with_path_traversal_cannot_escape(self, storage):
        response = self._post("evil.mp3", title="../../etc/passwd")
        assert response.status_code == 200
        stored = Path(response.json()["track"]["path"]).resolve()
        assert stored.is_relative_to((storage.parent / "music").resolve())
        assert ".." not in stored.name

    def test_unknown_mood_is_a_400(self, storage):
        assert self._post("a.mp3", mood="screaming").status_code == 400

    def test_empty_file_is_a_400(self, storage):
        assert self._post("a.mp3", content=b"").status_code == 400

    def test_uploaded_track_appears_in_the_library(self, storage):
        self._post("minha.mp3", title="minha", mood="dark")
        ids = [track["id"] for track in client.get("/api/music/tracks").json()["tracks"]]
        assert any(track_id.startswith("upload-minha") for track_id in ids)
        found = client.get("/api/music/search", params={"q": "minha"}).json()["tracks"]
        assert found and found[0]["title"] == "minha"


# ------------------------------------------------------------ /api/build-video
class TestBuildVideoWiring:
    @pytest.fixture(autouse=True)
    def _no_network(self, monkeypatch):
        monkeypatch.setattr(
            app_module, "search_media_for_scenes",
            lambda storyboard, **kw: ({0: [{"url": "https://x.test/a.jpg", "source": "Pexels",
                                           "keyword": "banknotes"}]}, "ai"),
        )
        monkeypatch.setattr(app_module, "search_media_for_keywords", lambda keywords: [])

    def _post(self, storage, renderer, **fields):
        (storage / "projeto.srt").write_text(SRT_TEXT, encoding="utf-8")
        data = {"project_name": "projeto"}
        data.update(fields)
        with mock.patch.object(app_module, "render_video_hyperframes", renderer):
            return client.post("/api/build-video", data=data)

    def test_forwards_theming_and_music_to_the_renderer(self, storage):
        renderer = mock.AsyncMock(return_value={"status": "rendered", "output_path": "x.mp4"})
        response = self._post(
            storage, renderer,
            preset="neon",
            subtitle_style=json.dumps({"font_size": 60, "uppercase": True}),
            music_track="dark-depths",
            music_volume="0.35",
            duck_voice="false",
        )
        assert response.status_code == 200
        _, kwargs = renderer.call_args
        assert kwargs["preset"] == "neon"
        assert kwargs["subtitle_style"] == {"font_size": 60, "uppercase": True}
        assert kwargs["music_track"] == "dark-depths"
        assert kwargs["music_volume"] == 0.35
        assert kwargs["duck_voice"] is False
        assert "media_pool" in kwargs

    def test_defaults_are_passed_when_the_new_fields_are_absent(self, storage):
        renderer = mock.AsyncMock(return_value={"status": "rendered"})
        assert self._post(storage, renderer).status_code == 200
        _, kwargs = renderer.call_args
        assert kwargs["preset"] == ""
        assert kwargs["subtitle_style"] is None
        assert kwargs["music_track"] == ""
        assert kwargs["music_volume"] == pytest.approx(0.18)
        assert kwargs["duck_voice"] is True

    def test_malformed_subtitle_style_json_is_a_400(self, storage):
        renderer = mock.AsyncMock(return_value={"status": "rendered"})
        response = self._post(storage, renderer, subtitle_style="{not json")
        assert response.status_code == 400
        assert response.json()["detail"]
        renderer.assert_not_called()

    def test_subtitle_style_must_be_an_object(self, storage):
        renderer = mock.AsyncMock(return_value={"status": "rendered"})
        assert self._post(storage, renderer, subtitle_style="[1, 2, 3]").status_code == 400
        renderer.assert_not_called()

    def test_invalid_music_volume_is_a_400(self, storage):
        renderer = mock.AsyncMock(return_value={"status": "rendered"})
        assert self._post(storage, renderer, music_volume="alto").status_code == 400
        renderer.assert_not_called()

    def test_music_volume_is_clamped(self, storage):
        renderer = mock.AsyncMock(return_value={"status": "rendered"})
        self._post(storage, renderer, music_volume="9")
        assert renderer.call_args[1]["music_volume"] == 1.0

    def test_response_keeps_every_existing_key(self, storage):
        renderer = mock.AsyncMock(return_value={"status": "rendered", "output_path": "x.mp4"})
        body = self._post(storage, renderer, preset="bold").json()
        assert BUILD_VIDEO_KEYS <= set(body)
        assert body["project_name"] == "projeto"
        assert body["render_real"] == body["render"]
        assert body["preset"] == "bold"
        # Additive keys for the UI.
        assert body["music"] is None
        assert "music_volume" in body and "duck_voice" in body

    def test_known_music_track_is_echoed_back(self, storage):
        seed = storage / "seed.mp3"
        seed.write_bytes(b"ID3seed-audio")
        registered = music_service.register_upload(seed, title="seed", mood="lofi")
        track_id = registered.id
        renderer = mock.AsyncMock(return_value={"status": "rendered"})
        body = self._post(storage, renderer, music_track=track_id).json()
        assert body["music"]["id"] == track_id
        assert body["music"]["mood"] == "lofi"


# ------------------------------------------------------------- /api/settings
class TestSettings:
    @pytest.fixture(autouse=True)
    def _restore_env(self):
        saved = dict(os.environ)
        yield
        os.environ.clear()
        os.environ.update(saved)

    def _payload(self, **overrides):
        payload = {
            "openrouter_api_key": "sk-or-test",
            "openrouter_model": "meta-llama/llama-3.3-8b-instruct:free",
            "pexels_api_key": "", "pixabay_api_key": "", "gemini_api_key": "",
            "openai_api_key": "", "youtube_api_key": "",
            "azure_speech_key": "azure-key-123",
            "azure_speech_region": "westeurope",
        }
        payload.update(overrides)
        return payload

    def test_persists_the_azure_speech_pair(self):
        with mock.patch("pathlib.Path.write_text") as write:
            response = client.post("/api/settings", json=self._payload())
        assert response.status_code == 200
        assert response.json()["status"] == "saved"
        written = write.call_args[0][0]
        assert "AZURE_SPEECH_KEY=azure-key-123" in written
        assert "AZURE_SPEECH_REGION=westeurope" in written
        # The existing keys are untouched.
        assert "OPENROUTER_API_KEY=sk-or-test" in written
        assert "OPENROUTER_MODEL=meta-llama/llama-3.3-8b-instruct:free" in written
        for key in ("PEXELS_API_KEY", "PIXABAY_API_KEY", "GEMINI_API_KEY",
                    "OPENAI_API_KEY", "YOUTUBE_API_KEY"):
            assert f"{key}=" in written
        assert os.environ["AZURE_SPEECH_KEY"] == "azure-key-123"
        assert os.environ["AZURE_SPEECH_REGION"] == "westeurope"

    def test_still_rejects_a_non_free_model_without_writing(self):
        with mock.patch("pathlib.Path.write_text") as write:
            response = client.post(
                "/api/settings", json=self._payload(openrouter_model="openai/gpt-4o")
            )
        assert response.status_code == 400
        assert ":free" in response.json()["detail"]
        write.assert_not_called()

    def test_a_payload_without_the_azure_fields_still_saves(self):
        payload = self._payload()
        payload["azure_speech_key"] = ""
        payload["azure_speech_region"] = ""
        with mock.patch("pathlib.Path.write_text") as write:
            response = client.post("/api/settings", json=payload)
        assert response.status_code == 200
        written = write.call_args[0][0]
        assert "AZURE_SPEECH_KEY=" in written
        assert "AZURE_SPEECH_REGION=" in written


# ---------------------------------------------------- degraded environment / CORS
class TestDegradedEnvironment:
    def test_app_imports_and_health_works_without_librosa(self):
        out = _subprocess(
            r"""
            import sys

            class _BlockLibrosa:
                def find_spec(self, name, path=None, target=None):
                    if name == "librosa" or name.startswith("librosa."):
                        raise ImportError("No module named 'librosa'")
                    return None

            sys.meta_path.insert(0, _BlockLibrosa())

            try:
                import librosa
                print("BLOCKER-FAILED")
            except ImportError:
                print("librosa-blocked")

            from fastapi.testclient import TestClient
            from backend.app import app

            from backend.services.pipeline import UPLOAD_DIR

            (UPLOAD_DIR / "x.srt").write_text(
                "1\n00:00:00,000 --> 00:00:02,500\nOla.\n", encoding="utf-8"
            )

            client = TestClient(app)
            print("health", client.get("/health").status_code)
            print("providers", client.get("/api/providers").status_code)
            print("presets", client.get("/api/presets").status_code)
            print("music", client.get("/api/music/tracks").status_code)
            print("tts-status", client.get("/api/tts/status").status_code)
            print("voices", client.get("/api/voices").status_code)
            print("languages", client.get("/api/languages").status_code)
            # The viral pipeline is the only thing that needs librosa: it must
            # report a missing dependency, not take the app down.
            print("viral", client.post("/api/build-viral", data={"project_name": "x"}).status_code)
            """
        )
        assert "librosa-blocked" in out
        assert "BLOCKER-FAILED" not in out
        for line in ("health 200", "providers 200", "presets 200", "music 200",
                     "tts-status 200", "voices 200", "languages 200"):
            assert line in out, out
        # 503, not a 500: the import failure is a missing optional dependency.
        assert "viral 503" in out, out

    def test_app_imports_without_edge_tts(self):
        out = _subprocess(
            """
            import sys

            class _BlockEdge:
                def find_spec(self, name, path=None, target=None):
                    if name == "edge_tts" or name.startswith("edge_tts."):
                        raise ImportError("No module named 'edge_tts'")
                    return None

            sys.meta_path.insert(0, _BlockEdge())

            from fastapi.testclient import TestClient
            from backend.app import app

            client = TestClient(app)
            status = client.get("/api/tts/status").json()
            print("status-code", client.get("/api/tts/status").status_code)
            print("edge-available", status["providers"]["edge"]["available"])
            """
        )
        assert "status-code 200" in out, out
        assert "edge-available False" in out, out

    def test_cors_origins_come_from_the_environment(self, monkeypatch):
        monkeypatch.setenv("DARK_STUDIO_ALLOWED_ORIGINS", "http://a.test, http://b.test")
        assert app_module.allowed_origins() == ["http://a.test", "http://b.test"]

    def test_cors_origins_default_to_the_localhost_pair(self, monkeypatch):
        monkeypatch.delenv("DARK_STUDIO_ALLOWED_ORIGINS", raising=False)
        assert app_module.allowed_origins() == [
            "http://127.0.0.1:8013", "http://localhost:8013",
        ]

    def test_cors_allows_any_localhost_port(self):
        response = client.get("/health", headers={"Origin": "http://127.0.0.1:54321"})
        assert response.headers.get("access-control-allow-origin") == "http://127.0.0.1:54321"

    def test_cors_honours_the_env_var_for_a_moved_port(self):
        out = _subprocess(
            """
            from fastapi.testclient import TestClient
            from backend.app import app

            client = TestClient(app)
            for origin in ("http://127.0.0.1:9123", "http://localhost:9123",
                           "https://studio.test", "http://evil.test"):
                response = client.get("/health", headers={"Origin": origin})
                print(origin, response.headers.get("access-control-allow-origin", "DENIED"))
            """,
            env_extra={"DARK_STUDIO_ALLOWED_ORIGINS": "https://studio.test"},
        )
        assert "http://127.0.0.1:9123 http://127.0.0.1:9123" in out, out
        assert "http://localhost:9123 http://localhost:9123" in out, out
        assert "https://studio.test https://studio.test" in out, out
        assert "http://evil.test DENIED" in out, out


class TestExistingSurfaceIsIntact:
    def test_health_still_answers(self):
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_frontend_mount_is_still_served(self):
        assert (REPO_ROOT / "frontend").exists()
        assert app_module.app.routes  # the /app mount survived the import changes

    def test_providers_endpoint_keeps_its_shape(self):
        body = client.get("/api/providers").json()
        assert "providers" in body
        assert "openrouter" in body["providers"]
        # Additive: the settings panel reads narration providers from here too.
        assert set(body["tts"]["providers"]) >= {"edge", "openai", "azure"}
