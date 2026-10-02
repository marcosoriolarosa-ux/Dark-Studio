"""Tests for the TTS service.

No network: edge_tts and httpx.post are both mocked. The point of these tests is
the parts the API layer depends on - a static voice catalogue it can render
offline, sentence-aware chunking that never clips a word, and provider failures
reported through the shared AUTH_* contract.
"""

from pathlib import Path
from unittest import mock

import pytest

from backend.services import tts
from backend.services.auth_contract import (
    AUTH_INVALID_KEY,
    AUTH_MISSING_KEY,
    AUTH_QUOTA_EXCEEDED,
    AUTH_RATE_LIMIT,
    AUTH_REQUEST_FAILED,
    AuthError,
)

REQUIRED_LOCALES = {"pt-PT", "pt-BR", "en-US", "en-GB", "es-ES", "fr-FR", "de-DE"}


ID3_MAGIC = b"ID3PAYLOAD"


def _id3(payload: bytes) -> bytes:
    """Wrap bytes in a real ID3v2 tag (synchsafe size) followed by the audio."""
    size = len(ID3_MAGIC)
    synchsafe = bytes(
        [
            (size >> 21) & 0x7F,
            (size >> 14) & 0x7F,
            (size >> 7) & 0x7F,
            size & 0x7F,
        ]
    )
    return b"ID3\x04\x00\x00" + synchsafe + ID3_MAGIC + payload


class FakeCommunicate:
    """Stand-in for edge_tts.Communicate that writes deterministic bytes."""

    instances: list = []

    def __init__(self, text, voice, rate="+0%", volume="+0%", pitch="+0Hz"):
        self.text = text
        self.voice = voice
        self.rate = rate
        self.volume = volume
        self.pitch = pitch
        FakeCommunicate.instances.append(self)

    async def save(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(_id3(self.text.encode("utf-8")))


class FakeEdgeModule:
    Communicate = FakeCommunicate


@pytest.fixture
def fake_edge(monkeypatch):
    FakeCommunicate.instances = []
    monkeypatch.setattr(tts, "edge_tts", FakeEdgeModule())
    return FakeCommunicate


@pytest.fixture
def no_edge(monkeypatch):
    monkeypatch.setattr(tts, "edge_tts", None)


@pytest.fixture(autouse=True)
def _clean_key_env(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("TTS_OPENAI_MODEL", raising=False)
    monkeypatch.delenv(tts.AZURE_KEY_ENV, raising=False)
    monkeypatch.delenv(tts.AZURE_REGION_ENV, raising=False)


def _response(status=200, content=b"audio-bytes", text="upstream said no"):
    response = mock.Mock()
    response.status_code = status
    response.content = content
    response.text = text
    response.json.return_value = {}
    return response


class TestVoiceCatalogue:
    def test_returns_more_than_forty_voices(self):
        assert len(tts.list_voices()) > 40

    def test_default_voice_is_present(self):
        assert tts.DEFAULT_VOICE in {v.id for v in tts.list_voices()}

    def test_covers_every_required_locale(self):
        locales = {v.locale for v in tts.list_voices()}
        assert REQUIRED_LOCALES.issubset(locales)

    def test_locale_prefix_filter(self):
        portuguese = tts.list_voices("pt")
        assert portuguese
        assert {v.locale for v in portuguese} == {"pt-PT", "pt-BR"}

    def test_exact_locale_filter(self):
        voices = tts.list_voices("pt-PT")
        assert voices
        assert all(v.locale == "pt-PT" for v in voices)

    def test_filter_is_case_insensitive(self):
        assert [v.id for v in tts.list_voices("PT-pt")] == [
            v.id for v in tts.list_voices("pt-PT")
        ]

    def test_unknown_locale_returns_empty_list(self):
        assert tts.list_voices("xx-YY") == []

    def test_is_deterministic_across_calls(self):
        first = [v.id for v in tts.list_voices()]
        second = [v.id for v in tts.list_voices()]
        assert first == second

    def test_result_is_a_copy_not_shared_state(self):
        voices = tts.list_voices()
        voices.clear()
        assert len(tts.list_voices()) > 40

    def test_ids_are_unique(self):
        ids = [v.id for v in tts.list_voices()]
        assert len(ids) == len(set(ids))

    @pytest.mark.parametrize("voice", tts.EDGE_VOICES)
    def test_every_id_matches_the_allowlist_pattern(self, voice):
        assert tts.VOICE_ID_PATTERN.match(voice.id)
        assert voice.provider == "edge"
        assert voice.gender in {"male", "female", "unknown"}
        assert voice.name


class TestSplitTextForSpeech:
    def test_empty_text(self):
        assert tts.split_text_for_speech("") == []

    def test_whitespace_only_text(self):
        assert tts.split_text_for_speech("   \n\t  ") == []

    def test_short_text_is_one_chunk(self):
        assert tts.split_text_for_speech("Uma frase curta.") == ["Uma frase curta."]

    def test_splits_on_sentence_boundaries(self):
        text = "Um dois tres. Quatro cinco seis. Sete."
        chunks = tts.split_text_for_speech(text, max_chars=20)
        assert chunks == ["Um dois tres.", "Quatro cinco seis.", "Sete."]

    def test_keeps_sentences_together_when_they_fit(self):
        text = "Um dois tres. Quatro cinco."
        assert tts.split_text_for_speech(text, max_chars=40) == [text]

    def test_respects_max_chars(self):
        text = " ".join(f"Frase numero {i} com conteudo suficiente." for i in range(12))
        for chunk in tts.split_text_for_speech(text, max_chars=120):
            assert len(chunk) <= 120

    def test_never_splits_mid_word(self):
        text = " ".join(f"palavra{i} do teste de audio em portugues" for i in range(40))
        chunks = tts.split_text_for_speech(text, max_chars=90)
        assert all(len(chunk) <= 90 for chunk in chunks)
        # Rejoining the chunks reproduces every original word: a boundary was only
        # ever placed on whitespace.
        assert " ".join(chunks).split() == text.split()

    def test_long_single_sentence_splits_on_word_boundaries(self):
        chunks = tts.split_text_for_speech("um dois tres quatro cinco seis", max_chars=14)
        assert chunks == ["um dois tres", "quatro cinco", "seis"]

    def test_word_longer_than_budget_is_hard_split(self):
        # No whitespace to cut on; the chunker must still make progress.
        chunks = tts.split_text_for_speech("x" * 50, max_chars=20)
        assert all(len(chunk) <= 20 for chunk in chunks)
        assert "".join(chunks) == "x" * 50

    def test_collapses_whitespace_so_output_matches_input_words(self):
        text = "Primeira  frase.\n\nSegunda\tfrase. Terceira frase."
        chunks = tts.split_text_for_speech(text, max_chars=25)
        assert " ".join(chunks).split() == text.split()

    def test_non_positive_max_chars_raises(self):
        with pytest.raises(ValueError):
            tts.split_text_for_speech("texto", max_chars=0)

    def test_default_budget_is_edge_safe(self):
        assert tts.split_text_for_speech("abc") == ["abc"]


class TestSynthesizeEdge:
    async def test_writes_audio_and_returns_path(self, fake_edge, tmp_path):
        target = tmp_path / "out" / "narration.mp3"
        result = await tts.synthesize_speech("ola mundo", target)
        assert result == target
        assert target.exists()
        assert target.read_bytes().startswith(b"ID3")

    async def test_creates_parent_directories(self, fake_edge, tmp_path):
        target = tmp_path / "deep" / "nested" / "narration.mp3"
        await tts.synthesize_speech("ola", target)
        assert target.parent.is_dir()

    async def test_forwards_voice_and_prosody(self, fake_edge, tmp_path):
        await tts.synthesize_speech(
            "ola", tmp_path / "n.mp3", "en-US-AriaNeural", rate="+10%", volume="-5%", pitch="+2Hz"
        )
        used = FakeCommunicate.instances[-1]
        assert used.voice == "en-US-AriaNeural"
        assert (used.rate, used.volume, used.pitch) == ("+10%", "-5%", "+2Hz")

    async def test_whitespace_is_collapsed_before_synthesis(self, fake_edge, tmp_path):
        await tts.synthesize_speech("  ola\n\n  mundo  ", tmp_path / "n.mp3")
        assert FakeCommunicate.instances[-1].text == "ola mundo"

    async def test_empty_text_raises_value_error(self, fake_edge, tmp_path):
        with pytest.raises(ValueError):
            await tts.synthesize_speech("   ", tmp_path / "n.mp3")

    async def test_unknown_voice_raises_before_any_call(self, fake_edge, tmp_path):
        with mock.patch("httpx.post") as post:
            with pytest.raises(ValueError, match="Unknown Edge voice"):
                await tts.synthesize_speech("ola", tmp_path / "n.mp3", "xx-YY-ZzzNeural")
        assert post.call_count == 0
        assert FakeCommunicate.instances == []

    @pytest.mark.parametrize(
        "bad_voice",
        [
            "../../v1/other",
            "pt-PT-RicardoNeural?x=1",
            "pt-PT-RicardoNeural/../../admin",
            "http://evil.example/pt-PT-XNeural",
            "pt-PT-ricardo neural",
            "",
        ],
    )
    async def test_unsafe_voice_ids_are_rejected(self, fake_edge, tmp_path, bad_voice):
        with mock.patch("httpx.post") as post:
            with pytest.raises(ValueError):
                await tts.synthesize_speech("ola", tmp_path / "n.mp3", bad_voice)
        post.assert_not_called()
        assert FakeCommunicate.instances == []

    async def test_unknown_provider_raises(self, fake_edge, tmp_path):
        with pytest.raises(ValueError, match="Unknown TTS provider"):
            await tts.synthesize_speech("ola", tmp_path / "n.mp3", provider="wat")

    async def test_edge_failure_becomes_request_failed(self, monkeypatch, tmp_path):
        class Boom:
            def __init__(self, *args, **kwargs):
                pass

            async def save(self, path):
                raise ConnectionError("socket closed")

        monkeypatch.setattr(tts, "edge_tts", mock.Mock(Communicate=Boom))
        with pytest.raises(AuthError) as excinfo:
            await tts.synthesize_speech("ola", tmp_path / "n.mp3")
        assert excinfo.value.error_code == AUTH_REQUEST_FAILED
        assert excinfo.value.status_code == 502

    async def test_missing_package_raises_named_runtime_error(self, no_edge, tmp_path):
        with pytest.raises(RuntimeError, match="edge-tts"):
            await tts.synthesize_speech("ola", tmp_path / "n.mp3")

    async def test_empty_edge_output_is_reported(self, monkeypatch, tmp_path):
        class Silent:
            def __init__(self, *args, **kwargs):
                pass

            async def save(self, path):
                Path(path).write_bytes(b"")

        monkeypatch.setattr(tts, "edge_tts", mock.Mock(Communicate=Silent))
        with pytest.raises(AuthError) as excinfo:
            await tts.synthesize_speech("ola", tmp_path / "n.mp3")
        assert excinfo.value.error_code == AUTH_REQUEST_FAILED


class TestProviderAvailability:
    def test_edge_available_when_package_imports(self, fake_edge):
        assert tts.is_available("edge") is True

    def test_edge_unavailable_without_package(self, no_edge):
        assert tts.is_available("edge") is False

    def test_unknown_provider_reports_false_without_raising(self):
        assert tts.is_available("nope") is False
        assert tts.is_available("") is False

    def test_openai_needs_a_key(self, monkeypatch):
        assert tts.is_available("openai") is False
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        assert tts.is_available("openai") is True

    def test_azure_needs_key_and_region(self, monkeypatch):
        monkeypatch.setenv(tts.AZURE_KEY_ENV, "azure-test")
        assert tts.is_available("azure") is False
        monkeypatch.setenv(tts.AZURE_REGION_ENV, "westeurope")
        assert tts.is_available("azure") is True

    def test_status_lists_every_provider(self):
        status = tts.get_tts_status()
        assert set(status["providers"]) == set(tts.SUPPORTED_PROVIDERS)
        assert status["default_voice"] == tts.DEFAULT_VOICE
        assert status["voices"] > 40


class TestKeyedProvidersMissingKey:
    async def test_openai_without_key_raises_missing_key(self, tmp_path):
        with mock.patch("httpx.post") as post:
            with pytest.raises(AuthError) as excinfo:
                await tts.synthesize_speech(
                    "ola", tmp_path / "n.mp3", "alloy", provider="openai"
                )
        assert excinfo.value.error_code == AUTH_MISSING_KEY
        assert excinfo.value.status_code == 401
        assert excinfo.value.details["provider"] == "openai"
        post.assert_not_called()

    async def test_azure_without_key_raises_missing_key(self, tmp_path):
        with mock.patch("httpx.post") as post:
            with pytest.raises(AuthError) as excinfo:
                await tts.synthesize_speech(
                    "ola", tmp_path / "n.mp3", "en-US-JennyNeural", provider="azure"
                )
        assert excinfo.value.error_code == AUTH_MISSING_KEY
        assert excinfo.value.details["provider"] == "azure"
        post.assert_not_called()

    async def test_azure_with_key_but_no_region_raises_missing_key(self, monkeypatch, tmp_path):
        monkeypatch.setenv(tts.AZURE_KEY_ENV, "azure-test")
        with pytest.raises(AuthError) as excinfo:
            await tts.synthesize_speech(
                "ola", tmp_path / "n.mp3", "en-US-JennyNeural", provider="azure"
            )
        assert excinfo.value.error_code == AUTH_MISSING_KEY


class TestOpenAIAdapter:
    @pytest.fixture(autouse=True)
    def _key(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    async def test_writes_returned_bytes(self, tmp_path):
        target = tmp_path / "n.mp3"
        with mock.patch("httpx.post", return_value=_response(content=b"mp3-audio")) as post:
            result = await tts.synthesize_speech("ola", target, "alloy", provider="openai")
        assert result == target
        assert target.read_bytes() == b"mp3-audio"
        assert post.call_args[1]["headers"]["Authorization"] == "Bearer sk-test"
        assert post.call_args[1]["json"]["voice"] == "alloy"

    async def test_default_edge_voice_falls_back_to_openai_default(self, tmp_path):
        with mock.patch("httpx.post", return_value=_response()) as post:
            await tts.synthesize_speech("ola", tmp_path / "n.mp3", provider="openai")
        assert post.call_args[1]["json"]["voice"] == tts.DEFAULT_OPENAI_VOICE

    async def test_unknown_openai_voice_rejected_before_request(self, tmp_path):
        with mock.patch("httpx.post") as post:
            with pytest.raises(ValueError, match="Unknown OpenAI voice"):
                await tts.synthesize_speech(
                    "ola", tmp_path / "n.mp3", "pt-PT-RicardoNeural", provider="openai"
                )
        post.assert_not_called()

    @pytest.mark.parametrize("status", [401, 403])
    async def test_invalid_key_statuses(self, tmp_path, status):
        with mock.patch("httpx.post", return_value=_response(status=status, text="bad key")):
            with pytest.raises(AuthError) as excinfo:
                await tts.synthesize_speech("ola", tmp_path / "n.mp3", "alloy", provider="openai")
        assert excinfo.value.error_code == AUTH_INVALID_KEY
        assert excinfo.value.status_code == status

    async def test_rate_limit_status(self, tmp_path):
        with mock.patch("httpx.post", return_value=_response(status=429, text="slow down")):
            with pytest.raises(AuthError) as excinfo:
                await tts.synthesize_speech("ola", tmp_path / "n.mp3", "alloy", provider="openai")
        assert excinfo.value.error_code == AUTH_RATE_LIMIT
        assert excinfo.value.status_code == 429

    async def test_quota_exceeded_status(self, tmp_path):
        with mock.patch("httpx.post", return_value=_response(status=402, text="no credits")):
            with pytest.raises(AuthError) as excinfo:
                await tts.synthesize_speech("ola", tmp_path / "n.mp3", "alloy", provider="openai")
        assert excinfo.value.error_code == AUTH_QUOTA_EXCEEDED

    async def test_server_error_maps_to_request_failed(self, tmp_path):
        with mock.patch("httpx.post", return_value=_response(status=503, text="down")):
            with pytest.raises(AuthError) as excinfo:
                await tts.synthesize_speech("ola", tmp_path / "n.mp3", "alloy", provider="openai")
        assert excinfo.value.error_code == AUTH_REQUEST_FAILED
        assert excinfo.value.status_code == 502

    async def test_network_error_maps_to_request_failed(self, tmp_path):
        import httpx as real_httpx

        with mock.patch("httpx.post", side_effect=real_httpx.ConnectError("down")):
            with pytest.raises(AuthError) as excinfo:
                await tts.synthesize_speech("ola", tmp_path / "n.mp3", "alloy", provider="openai")
        assert excinfo.value.error_code == AUTH_REQUEST_FAILED


class TestAzureAdapter:
    @pytest.fixture(autouse=True)
    def _keys(self, monkeypatch):
        monkeypatch.setenv(tts.AZURE_KEY_ENV, "azure-test")
        monkeypatch.setenv(tts.AZURE_REGION_ENV, "westeurope")

    async def test_posts_ssml_to_the_region_endpoint(self, tmp_path):
        target = tmp_path / "n.mp3"
        with mock.patch("httpx.post", return_value=_response(content=b"az-audio")) as post:
            result = await tts.synthesize_speech(
                "ola & tudo bem", target, "pt-PT-RicardoNeural", provider="azure"
            )
        assert target.read_bytes() == b"az-audio"
        assert result == target
        url = post.call_args[0][0]
        assert url == "https://westeurope.tts.speech.microsoft.com/cognitiveservices/v1"
        headers = post.call_args[1]["headers"]
        assert headers["Ocp-Apim-Subscription-Key"] == "azure-test"
        assert headers["X-Microsoft-OutputFormat"] == tts.AZURE_OUTPUT_FORMAT

    async def test_ssml_escapes_the_text(self, tmp_path):
        with mock.patch("httpx.post", return_value=_response()) as post:
            await tts.synthesize_speech(
                "a & b <c>", tmp_path / "n.mp3", "en-US-JennyNeural", provider="azure"
            )
        body = post.call_args[1]["content"].decode("utf-8")
        assert "a &amp; b &lt;c&gt;" in body
        assert "en-US-JennyNeural" in body

    async def test_default_edge_voice_falls_back_to_azure_default(self, tmp_path):
        with mock.patch("httpx.post", return_value=_response()) as post:
            await tts.synthesize_speech("ola", tmp_path / "n.mp3", provider="azure")
        assert tts.DEFAULT_AZURE_VOICE in post.call_args[1]["content"].decode("utf-8")

    @pytest.mark.parametrize("status", [401, 403])
    async def test_invalid_key_statuses(self, tmp_path, status):
        with mock.patch("httpx.post", return_value=_response(status=status, text="nope")):
            with pytest.raises(AuthError) as excinfo:
                await tts.synthesize_speech(
                    "ola", tmp_path / "n.mp3", "en-US-JennyNeural", provider="azure"
                )
        assert excinfo.value.error_code == AUTH_INVALID_KEY

    async def test_rate_limit_status(self, tmp_path):
        with mock.patch("httpx.post", return_value=_response(status=429, text="slow")):
            with pytest.raises(AuthError) as excinfo:
                await tts.synthesize_speech(
                    "ola", tmp_path / "n.mp3", "en-US-JennyNeural", provider="azure"
                )
        assert excinfo.value.error_code == AUTH_RATE_LIMIT

    async def test_quota_exceeded_status(self, tmp_path):
        with mock.patch("httpx.post", return_value=_response(status=402, text="quota")):
            with pytest.raises(AuthError) as excinfo:
                await tts.synthesize_speech(
                    "ola", tmp_path / "n.mp3", "en-US-JennyNeural", provider="azure"
                )
        assert excinfo.value.error_code == AUTH_QUOTA_EXCEEDED

    async def test_unsafe_azure_voice_is_rejected(self, tmp_path):
        with mock.patch("httpx.post") as post:
            with pytest.raises(ValueError):
                await tts.synthesize_speech(
                    "ola", tmp_path / "n.mp3", "../../evil", provider="azure"
                )
        post.assert_not_called()


class TestSynthesizeLong:
    async def test_single_chunk_delegates_to_synthesize_speech(self, fake_edge, tmp_path):
        target = tmp_path / "n.mp3"
        result = await tts.synthesize_speech_long("Uma frase so.", target)
        assert result == target
        assert len(FakeCommunicate.instances) == 1

    async def test_long_script_is_chunked_and_joined(self, fake_edge, tmp_path):
        text = " ".join(f"Frase numero {i} com conteudo suficiente." for i in range(30))
        target = tmp_path / "n.mp3"
        await tts.synthesize_speech_long(text, target, max_chars=120)
        assert len(FakeCommunicate.instances) > 1
        payload = target.read_bytes()
        for used in FakeCommunicate.instances:
            assert used.text.encode("utf-8") in payload

    async def test_joins_drop_intermediate_id3_tags(self, fake_edge, tmp_path):
        target = tmp_path / "n.mp3"
        await tts.synthesize_speech_long(
            " ".join(f"Frase numero {i} com conteudo suficiente." for i in range(20)),
            target,
            max_chars=100,
        )
        # Only the first chunk carries a tag; a tag mid-stream is what makes naive
        # concatenation glitch.
        assert target.read_bytes().count(ID3_MAGIC) == 1

    async def test_creates_parent_dirs_and_cleans_temp_files(self, fake_edge, tmp_path):
        target = tmp_path / "nested" / "n.mp3"
        await tts.synthesize_speech_long(
            " ".join(f"Frase numero {i} com conteudo suficiente." for i in range(20)),
            target,
            max_chars=100,
        )
        assert target.exists()
        assert [p.name for p in target.parent.iterdir()] == ["n.mp3"]

    async def test_empty_text_raises_value_error(self, fake_edge, tmp_path):
        with pytest.raises(ValueError):
            await tts.synthesize_speech_long("   ", tmp_path / "n.mp3")

    async def test_temp_dir_is_removed_when_a_chunk_fails(self, monkeypatch, tmp_path):
        calls = {"n": 0}

        class FlakyCommunicate:
            def __init__(self, text, voice, rate="+0%", volume="+0%", pitch="+0Hz"):
                self.text = text

            async def save(self, path):
                calls["n"] += 1
                if calls["n"] == 2:
                    raise ConnectionError("dropped")
                Path(path).write_bytes(b"chunk")

        monkeypatch.setattr(tts, "edge_tts", mock.Mock(Communicate=FlakyCommunicate))
        target = tmp_path / "n.mp3"
        with pytest.raises(AuthError):
            await tts.synthesize_speech_long(
                " ".join(f"Frase numero {i} com conteudo suficiente." for i in range(20)),
                target,
                max_chars=100,
            )
        assert list(target.parent.iterdir()) == []


class TestStripId3:
    def test_removes_a_valid_leading_tag(self):
        payload = tts._strip_id3(_id3(b"audio"))
        assert payload == b"audio"

    def test_keeps_audio_when_the_tag_claims_to_be_huge(self):
        bogus = b"ID3\x04\x00\x00\x7f\x7f\x7f\x7f" + b"audio"
        assert tts._strip_id3(bogus) == bogus

    def test_drops_a_trailing_id3v1_tag(self):
        assert tts._strip_id3(b"audio" + b"TAG" + b"x" * 125) == b"audio"
