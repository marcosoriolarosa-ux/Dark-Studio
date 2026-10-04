"""Tests for the TTS service.

No network: edge_tts, its ``list_voices()`` and httpx.post are all mocked or pinned
offline by the autouse ``_offline_catalogue`` fixture. That pinning is load-bearing -
without it the suite would race the network and ``len(list_voices())`` would mean 41
on an offline machine and 314 on a connected one.

The points the API layer depends on: a voice catalogue it can render offline *and*
one that tracks upstream when it can reach it, sentence-aware chunking that never
clips a word, an unknown voice id reported as its own error rather than as a
credential failure, and the pre-existing AUTH_* credential contract left intact.
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
from backend.services.tts import TTS_UNKNOWN_VOICE

REQUIRED_LOCALES = {"pt-PT", "pt-BR", "en-US", "en-GB", "es-ES", "fr-FR", "de-DE"}

# The ids that were shipped as live and are not any more. They caused the outage, so
# they are pinned here: if any of them reappears in the catalogue, it is back in.
RETIRED_VOICE_IDS = (
    "pt-PT-RicardoMultilingualNeural",
    "pt-PT-DuarteMultilingualNeural",
    "pt-PT-FernandaMultilingualNeural",
    "pt-PT-RicardoNeural",
    "pt-PT-FernandaNeural",
    "pt-PT-InesNeural",
    "pt-BR-RicardoNeural",
    "pt-BR-FernandaNeural",
    "es-ES-ElenaNeural",
    "fr-FR-ThomasNeural",
    "de-DE-ConradMultilingualNeural",
)


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


@pytest.fixture(autouse=True)
def _offline_catalogue(monkeypatch):
    """Pin every test to the offline snapshot unless it asks for the live one."""
    monkeypatch.setattr(tts, "_live_voice_catalogue", lambda refresh=False: None)
    tts.reset_live_catalogue_cache()


def _live_record(short_name, locale=None, gender="Female", status="GA"):
    """One entry shaped like a real edge_tts.list_voices() record.

    Upstream drops the trailing "Neural" from FriendlyName but keeps "Multilingual"
    ("Microsoft Duarte Online (Natural) - Portuguese (Portugal)", "Microsoft
    RemyMultilingual Online (Natural) - French (France)"), so this does too - a
    fixture that invented a different convention would not prove the parser.
    """
    locale = locale or "-".join(short_name.split("-")[:2])
    name = short_name[len(locale) + 1:]
    name = name[: -len("Neural")] if name.endswith("Neural") else name
    friendly = f"Microsoft {name} Online (Natural) - {locale}"
    return {
        "Name": friendly,
        "ShortName": short_name,
        "Gender": gender,
        "Locale": locale,
        "SuggestedCodec": "audio-24khz-48kbitrate-mono-mp3",
        "FriendlyName": friendly,
        "Status": status,
    }


# Mirrors the shape of the 2026-10-04 live catalogue, including the three id families
# upstream carries that this app must not offer.
LIVE_RECORDS = [
    _live_record("pt-PT-DuarteNeural", gender="Male"),
    _live_record("pt-PT-RaquelNeural", gender="Female"),
    _live_record("pt-BR-AntonioNeural", gender="Male"),
    _live_record("en-US-AriaNeural"),
    _live_record("en-GB-RyanNeural", gender="Male"),
    _live_record("es-ES-XimenaNeural"),
    _live_record("fr-FR-VivienneMultilingualNeural"),
    _live_record("de-DE-ConradNeural", gender="Male"),
    # Filtered: three-letter language, extra region subtag, script subtag, deprecated.
    _live_record("fil-PH-AngeloNeural", locale="fil-PH", gender="Male"),
    _live_record("zh-CN-liaoning-XiaobeiNeural", locale="zh-CN-liaoning", gender="Female"),
    _live_record("iu-Latn-CA-SiqiniqNeural", locale="iu-Latn-CA", gender="Female"),
    _live_record("en-GB-MaisieNeural", status="Deprecated"),
]


def _live_raw():
    """Every record through the normaliser, including the ones it rejects."""
    return [tts._voice_from_live(record) for record in LIVE_RECORDS]


def _live_catalogue():
    return [voice for voice in _live_raw() if voice is not None]


def _live_ids():
    return {voice.id for voice in _live_catalogue()}


# Captured before the autouse fixture swaps it out, for the tests that exercise the
# real cache/timeout/fetch logic instead of a canned list.
_REAL_LIVE_CATALOGUE = tts._live_voice_catalogue


@pytest.fixture
def real_live(monkeypatch):
    """Undo the offline pin so the genuine fetch path runs, still with no network."""
    monkeypatch.setattr(tts, "_live_voice_catalogue", _REAL_LIVE_CATALOGUE)
    tts.reset_live_catalogue_cache()


@pytest.fixture
def fake_live(monkeypatch):
    """Make the live catalogue answer from ``LIVE_RECORDS``, no network."""
    calls = []

    def _catalogue(refresh=False):
        calls.append(refresh)
        return _live_catalogue()

    monkeypatch.setattr(tts, "_live_voice_catalogue", _catalogue)
    return calls


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


class TestDefaultVoice:
    """DEFAULT_VOICE must be a voice that exists. Proven without a network.

    The check that actually matters upstream cannot run here, so it is proxied by
    this module's own catalogue data plus the id grammar: the default has to be in
    the verified offline snapshot, has to parse as an Edge short name, has to carry
    the locale the product narrates in, and the snapshot itself has to be free of
    every id the live catalogue no longer lists. That combination is what fails if
    someone hand-edits the catalogue again.
    """

    def test_default_is_in_the_offline_catalogue(self):
        assert tts.DEFAULT_VOICE in {voice.id for voice in tts.EDGE_VOICES}

    def test_default_is_well_formed_for_the_request_layer(self):
        assert tts.VOICE_ID_PATTERN.match(tts.DEFAULT_VOICE)

    def test_default_is_a_portuguese_portugal_voice(self):
        default = next(v for v in tts.EDGE_VOICES if v.id == tts.DEFAULT_VOICE)
        assert default.locale == "pt-PT"
        assert default.provider == "edge"
        assert default.name

    def test_default_is_accepted_by_validation_with_no_network(self, tmp_path):
        # _validate_voice with the live catalogue unreachable is the offline promise.
        assert tts._validate_voice(tts.DEFAULT_VOICE, "edge") == tts.DEFAULT_VOICE

    def test_default_is_accepted_by_validation_with_the_live_catalogue(self, fake_live):
        assert tts.DEFAULT_VOICE in _live_ids()
        assert tts._validate_voice(tts.DEFAULT_VOICE, "edge") == tts.DEFAULT_VOICE

    @pytest.mark.parametrize("retired", RETIRED_VOICE_IDS)
    def test_retired_ids_are_absent_from_the_catalogue(self, retired):
        assert retired not in {voice.id for voice in tts.EDGE_VOICES}
        with pytest.raises(AuthError) as excinfo:
            tts._validate_voice(retired, "edge")
        assert excinfo.value.error_code == TTS_UNKNOWN_VOICE

    def test_the_shipped_default_is_no_longer_a_pt_pt_choice(self):
        # The retired id must be gone from the data *and* unaccepted by validation.
        # Regression lock for the actual outage: the id that killed a one-click run
        # must not come back as a default, and must not be offered.
        assert tts.DEFAULT_VOICE != "pt-PT-RicardoMultilingualNeural"
        offered = {voice.id for voice in tts.list_voices()}
        assert "pt-PT-RicardoMultilingualNeural" not in offered

    def test_snapshot_date_is_stamped(self):
        assert tts.EDGE_VOICES_SNAPSHOT_DATE == "2026-10-04"
        year, month, day = (int(part) for part in tts.EDGE_VOICES_SNAPSHOT_DATE.split("-"))
        assert (year, month, day) == (2026, 10, 4)


class TestLiveCatalogue:
    def test_live_list_wins_when_it_answers(self, fake_live):
        offered = {voice.id for voice in tts.list_voices()}
        assert offered == _live_ids()
        # Not the snapshot: live-first is the point of the whole change.
        assert offered != tts.snapshot_voice_ids()

    def test_live_only_offers_ids_the_request_layer_accepts(self, fake_live):
        # The picker must never be wider than _validate_voice, or it hands the user
        # an id the app itself refuses.
        offered = tts.list_voices()
        assert offered
        for voice in offered:
            assert tts.VOICE_ID_PATTERN.match(voice.id)
            assert tts._validate_voice(voice.id, "edge") == voice.id

    def test_ids_the_pattern_rejects_are_dropped(self, fake_live):
        offered = {voice.id for voice in tts.list_voices()}
        assert "fil-PH-AngeloNeural" not in offered
        assert "zh-CN-liaoning-XiaobeiNeural" not in offered
        assert "iu-Latn-CA-SiqiniqNeural" not in offered

    def test_deprecated_voices_are_not_offered(self, fake_live):
        assert "en-GB-MaisieNeural" not in {voice.id for voice in tts.list_voices()}

    def test_live_result_is_sorted_and_stable(self, fake_live):
        ids = [voice.id for voice in tts.list_voices()]
        assert ids == sorted(ids)
        assert ids == [voice.id for voice in tts.list_voices()]

    def test_live_records_become_useful_voice_info(self, fake_live):
        duarte = next(v for v in tts.list_voices() if v.id == "pt-PT-DuarteNeural")
        assert (duarte.name, duarte.gender, duarte.locale) == ("Duarte", "male", "pt-PT")

    def test_display_name_falls_back_to_the_id(self):
        record = _live_record("en-US-AriaNeural")
        record["FriendlyName"] = ""
        assert tts._live_display_name(record, "en-US-AriaNeural") == "AriaNeural"

    def test_a_broken_record_becomes_none_rather_than_raising(self):
        assert tts._voice_from_live({"ShortName": "", "Locale": "pt-PT"}) is None
        assert tts._voice_from_live({}) is None

    def test_the_fixture_itself_exercises_every_filter(self, fake_live):
        # If this stops holding, the "filtered" tests above are proving nothing.
        assert len(_live_raw()) > len(_live_catalogue())
        assert None in _live_raw()

    def test_locale_filter_applies_to_the_live_catalogue(self, fake_live):
        assert {voice.locale for voice in tts.list_voices("pt")} == {"pt-PT", "pt-BR"}
        assert all(v.locale == "pt-PT" for v in tts.list_voices("pt-PT"))
        assert tts.list_voices("xx-YY") == []


class TestLiveCatalogueFallback:
    """With no live list, the app must still start and still list voices."""

    def test_offline_list_never_offers_an_id_outside_the_snapshot(self):
        offered = {voice.id for voice in tts.list_voices()}
        snapshot = {voice.id for voice in tts.EDGE_VOICES}
        assert offered == snapshot

    def test_offline_list_is_never_wider_than_what_validation_accepts(self):
        for voice in tts.list_voices():
            assert tts.VOICE_ID_PATTERN.match(voice.id)
            assert tts._validate_voice(voice.id, "edge") == voice.id

    def test_offline_list_still_serves_the_default_and_every_locale(self):
        assert tts.DEFAULT_VOICE in {voice.id for voice in tts.list_voices()}
        assert REQUIRED_LOCALES <= {voice.locale for voice in tts.list_voices()}
        assert len(tts.list_voices()) > 40

    def test_real_fetch_failure_returns_none_rather_than_raising(self, real_live, monkeypatch):
        def _boom():
            raise RuntimeError("no network")

        monkeypatch.setattr(tts.edge_tts, "list_voices", _boom)
        tts.reset_live_catalogue_cache()
        assert tts._live_voice_catalogue() is None
        assert tts.confirmed_edge_voice_ids() is None
        assert len(tts.list_voices()) == len(tts.EDGE_VOICES)

    def test_a_failure_is_remembered_so_offline_does_not_stall_every_call(
        self, real_live, monkeypatch
    ):
        calls = []

        def _counter():
            calls.append(1)
            raise RuntimeError("no network")

        monkeypatch.setattr(tts.edge_tts, "list_voices", _counter)
        tts.reset_live_catalogue_cache()
        for _ in range(5):
            assert tts._live_voice_catalogue() is None
        assert len(calls) == 1

    def test_a_successful_fetch_is_cached_for_the_ttl(self, real_live, monkeypatch):
        calls = []

        async def _list():
            calls.append(1)
            return [_live_record("pt-PT-RaquelNeural", gender="Female")]

        monkeypatch.setattr(tts.edge_tts, "list_voices", _list)
        tts.reset_live_catalogue_cache()
        for _ in range(5):
            assert [v.id for v in tts.list_voices()] == ["pt-PT-RaquelNeural"]
        assert len(calls) == 1

    def test_edge_tts_without_a_lister_yields_the_snapshot(self, real_live, monkeypatch):
        monkeypatch.setattr(tts, "edge_tts", FakeEdgeModule())  # Communicate only
        tts.reset_live_catalogue_cache()
        assert tts._live_voice_catalogue() is None
        assert len(tts.list_voices()) == len(tts.EDGE_VOICES)

    def test_missing_edge_tts_package_yields_the_snapshot(self, real_live, no_edge):
        tts.reset_live_catalogue_cache()
        assert tts._live_voice_catalogue() is None
        assert len(tts.list_voices()) == len(tts.EDGE_VOICES)


class TestUnknownVoiceError:
    """An id absent from the catalogue is its own error, never a credential error."""

    @pytest.fixture
    def unknown(self):
        return "pt-PT-RicardoMultilingualNeural"  # the id that broke production

    async def test_unknown_voice_is_not_auth_request_failed(self, fake_edge, tmp_path, unknown):
        with pytest.raises(AuthError) as excinfo:
            await tts.synthesize_speech("ola", tmp_path / "n.mp3", unknown)
        assert excinfo.value.error_code == TTS_UNKNOWN_VOICE
        assert excinfo.value.error_code != AUTH_REQUEST_FAILED
        assert excinfo.value.status_code == 400
        assert excinfo.value.status_code != 502
        assert FakeCommunicate.instances == []

    async def test_unknown_voice_never_reaches_the_network(self, fake_edge, tmp_path, unknown):
        with mock.patch("httpx.post") as post:
            with pytest.raises(AuthError) as excinfo:
                await tts.synthesize_speech("ola", tmp_path / "n.mp3", unknown)
        post.assert_not_called()
        assert excinfo.value.error_code == TTS_UNKNOWN_VOICE

    def test_message_names_the_voice_and_the_endpoint(self, unknown):
        with pytest.raises(AuthError) as excinfo:
            tts._validate_voice(unknown, "edge")
        message = excinfo.value.message
        assert unknown in message
        assert "GET /api/voices" in message
        assert tts.DEFAULT_VOICE in message

    def test_details_carry_the_actionable_fields(self, unknown):
        with pytest.raises(AuthError) as excinfo:
            tts._validate_voice(unknown, "edge")
        details = excinfo.value.details
        assert details["voice"] == unknown
        assert details["provider"] == "edge"
        assert details["voices_endpoint"] == "GET /api/voices"
        assert details["default_voice"] == tts.DEFAULT_VOICE
        assert details["catalogue_verified_live"] is False  # offline in this suite

    def test_offline_message_does_not_claim_to_have_checked(self, unknown):
        with pytest.raises(AuthError) as excinfo:
            tts._validate_voice(unknown, "edge")
        assert "could not be reached" in excinfo.value.message
        assert excinfo.value.details["catalogue_verified_live"] is False

    def test_live_reachable_says_it_is_not_in_the_catalogue(self, fake_live, unknown):
        with pytest.raises(AuthError) as excinfo:
            tts._validate_voice(unknown, "edge")
        assert excinfo.value.error_code == TTS_UNKNOWN_VOICE
        assert excinfo.value.details["catalogue_verified_live"] is True
        assert "not in the current Edge voice catalogue" in excinfo.value.message

    def test_a_live_voice_missing_from_the_snapshot_is_still_accepted(
        self, real_live, monkeypatch
    ):
        # The live catalogue is wider than the snapshot; it must not be second-guessed.
        async def _list():
            return [_live_record("pt-PT-ZeliaNeural", gender="Female")]

        monkeypatch.setattr(tts.edge_tts, "list_voices", _list)
        tts.reset_live_catalogue_cache()
        assert "pt-PT-ZeliaNeural" not in {v.id for v in tts.EDGE_VOICES}
        assert tts._validate_voice("pt-PT-ZeliaNeural", "edge") == "pt-PT-ZeliaNeural"

    def test_malformed_ids_stay_plain_value_errors(self):
        # Unchanged: an id no provider could ever satisfy is a client bug, not a
        # catalogue lookup, and it must not grow the new code.
        for bad in ("../../v1/other", "pt-PT-ricardo neural", "not-a-voice", "  "):
            with pytest.raises(ValueError) as excinfo:
                tts._validate_voice(bad, "edge")
            assert not isinstance(excinfo.value, AuthError)

    @pytest.mark.parametrize("silent", ["raises", "empty"])
    async def test_an_id_we_have_no_record_of_is_reported_as_an_unknown_voice(
        self, monkeypatch, tmp_path, silent, unknown
    ):
        # The only way to reach synthesis with an id we hold no record of is a
        # catalogue that changed under us mid-run. Then an empty stream really does
        # mean the id, and it gets the id's own error rather than a 502 about
        # credentials. This is the production path the fix has to cover.
        class NoAudio:
            def __init__(self, *args, **kwargs):
                pass

            async def save(self, path):
                if silent == "raises":
                    raise type(
                        "NoAudioReceived", (Exception,), {}
                    )("No audio was received. Please verify that your parameters are correct.")
                Path(path).write_bytes(b"")

        monkeypatch.setattr(tts, "edge_tts", mock.Mock(Communicate=NoAudio))
        monkeypatch.setattr(tts, "_validate_voice", lambda voice, provider: voice)
        with pytest.raises(AuthError) as excinfo:
            await tts.synthesize_speech("ola", tmp_path / "n.mp3", unknown)
        assert excinfo.value.error_code == TTS_UNKNOWN_VOICE
        assert excinfo.value.error_code != AUTH_REQUEST_FAILED
        assert excinfo.value.status_code == 400
        assert unknown in excinfo.value.message
        assert "GET /api/voices" in excinfo.value.message

    @pytest.mark.parametrize("silent", ["raises", "empty"])
    async def test_an_unconfirmed_but_known_voice_keeps_the_gateway_failure(
        self, monkeypatch, tmp_path, silent
    ):
        # The mirror image, and the guard against over-correcting. With the live
        # catalogue unreachable we cannot tell a retired id from a prosody Edge
        # disliked, and the id IS in our verified snapshot - so guessing "unknown
        # voice" would be over-confident mislabelling pointed the other way. The
        # AUTH_REQUEST_FAILED/502 contract stays, and the message says what we could
        # not check and where to look.
        class NoAudio:
            def __init__(self, *args, **kwargs):
                pass

            async def save(self, path):
                if silent == "raises":
                    raise type(
                        "NoAudioReceived", (Exception,), {}
                    )("No audio was received. Please verify that your parameters are correct.")
                Path(path).write_bytes(b"")

        monkeypatch.setattr(tts, "edge_tts", mock.Mock(Communicate=NoAudio))
        with pytest.raises(AuthError) as excinfo:
            await tts.synthesize_speech("ola", tmp_path / "n.mp3")
        assert excinfo.value.error_code == AUTH_REQUEST_FAILED
        assert excinfo.value.status_code == 502
        assert excinfo.value.details["voice"] == tts.DEFAULT_VOICE
        assert excinfo.value.details["voices_endpoint"] == "GET /api/voices"
        assert "catalogue could not be reached" in excinfo.value.message

    async def test_a_confirmed_live_voice_returning_nothing_stays_a_gateway_failure(
        self, monkeypatch, fake_live, tmp_path
    ):
        # With the catalogue in hand and the id in it, an empty stream is a
        # prosody/provider problem, so it keeps the pre-existing contract.
        class Silent:
            def __init__(self, *args, **kwargs):
                pass

            async def save(self, path):
                Path(path).write_bytes(b"")

        monkeypatch.setattr(tts, "edge_tts", mock.Mock(Communicate=Silent))
        with pytest.raises(AuthError) as excinfo:
            await tts.synthesize_speech("ola", tmp_path / "n.mp3")
        assert excinfo.value.error_code == AUTH_REQUEST_FAILED
        assert excinfo.value.status_code == 502

    async def test_a_connection_error_is_still_a_gateway_failure(self, monkeypatch, tmp_path):
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

    def test_the_new_code_does_not_collide_with_a_credential_code(self):
        from backend.services import auth_contract

        credential_codes = {
            auth_contract.AUTH_MISSING_KEY,
            auth_contract.AUTH_INVALID_KEY,
            auth_contract.AUTH_RATE_LIMIT,
            auth_contract.AUTH_QUOTA_EXCEEDED,
            auth_contract.AUTH_REQUEST_FAILED,
            auth_contract.AUTH_MODEL_NOT_FREE,
        }
        assert TTS_UNKNOWN_VOICE not in credential_codes


class TestCredentialContractIntact:
    """The new code was added, not swapped in: every credential code still maps."""

    def test_contract_constants_keep_their_values(self):
        from backend.services import auth_contract

        assert auth_contract.AUTH_MISSING_KEY == "AUTH_MISSING_KEY"
        assert auth_contract.AUTH_INVALID_KEY == "AUTH_INVALID_KEY"
        assert auth_contract.AUTH_RATE_LIMIT == "AUTH_RATE_LIMIT"
        assert auth_contract.AUTH_QUOTA_EXCEEDED == "AUTH_QUOTA_EXCEEDED"
        assert auth_contract.AUTH_REQUEST_FAILED == "AUTH_REQUEST_FAILED"
        assert auth_contract.AUTH_MODEL_NOT_FREE == "AUTH_MODEL_NOT_FREE"

    def test_tts_still_re_exports_every_credential_code(self):
        for code in (
            AUTH_MISSING_KEY,
            AUTH_INVALID_KEY,
            AUTH_RATE_LIMIT,
            AUTH_QUOTA_EXCEEDED,
            AUTH_REQUEST_FAILED,
        ):
            assert code in tts.__all__
            assert getattr(tts, code) == code

    async def test_missing_openai_key_is_a_missing_key_401(self, tmp_path):
        with mock.patch("httpx.post") as post:
            with pytest.raises(AuthError) as excinfo:
                await tts.synthesize_speech(
                    "ola", tmp_path / "n.mp3", "alloy", provider="openai"
                )
        assert excinfo.value.error_code == AUTH_MISSING_KEY
        assert excinfo.value.status_code == 401
        post.assert_not_called()

    @pytest.mark.parametrize("status", [401, 403])
    @pytest.mark.parametrize(
        "provider,voice", [("openai", "alloy"), ("azure", "en-US-JennyNeural")]
    )
    async def test_rejected_credentials_keep_their_codes(
        self, monkeypatch, tmp_path, provider, voice, status
    ):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.setenv(tts.AZURE_KEY_ENV, "azure-test")
        monkeypatch.setenv(tts.AZURE_REGION_ENV, "westeurope")
        with mock.patch("httpx.post", return_value=_response(status=status, text="bad key")):
            with pytest.raises(AuthError) as excinfo:
                await tts.synthesize_speech(
                    "ola", tmp_path / "n.mp3", voice, provider=provider
                )
        assert excinfo.value.error_code == AUTH_INVALID_KEY
        assert excinfo.value.status_code == status

    @pytest.mark.parametrize(
        "upstream,expected", [(429, AUTH_RATE_LIMIT), (402, AUTH_QUOTA_EXCEEDED)]
    )
    async def test_rate_limit_and_quota_keep_their_codes(
        self, monkeypatch, tmp_path, upstream, expected
    ):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        with mock.patch("httpx.post", return_value=_response(status=upstream, text="no")):
            with pytest.raises(AuthError) as excinfo:
                await tts.synthesize_speech(
                    "ola", tmp_path / "n.mp3", "alloy", provider="openai"
                )
        assert excinfo.value.error_code == expected

    async def test_an_unknown_voice_on_a_keyed_provider_is_not_a_credential_error(
        self, monkeypatch, tmp_path
    ):
        # A retired Edge id forwarded to a keyed provider must still be rejected as
        # an unknown id, never as a key problem.
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        with mock.patch("httpx.post") as post:
            with pytest.raises(ValueError) as excinfo:
                await tts.synthesize_speech(
                    "ola", tmp_path / "n.mp3", "pt-PT-RicardoMultilingualNeural",
                    provider="openai",
                )
        post.assert_not_called()
        assert not isinstance(excinfo.value, AuthError)


class TestTtsStatusHonesty:
    def test_status_reports_the_catalogue_it_is_actually_serving(self):
        assert tts.get_tts_status()["voices"] == len(tts.list_voices())

    def test_offline_status_says_it_is_not_verified(self):
        status = tts.get_tts_status()
        assert status["voices"] == len(tts.EDGE_VOICES)
        assert status["voice_source"] == "offline_snapshot"
        assert status["voice_catalogue_verified"] is False
        assert status["voice_catalogue_snapshot_date"] == tts.EDGE_VOICES_SNAPSHOT_DATE
        note = status["voice_catalogue_note"]
        assert "unreachable" in note
        assert "not a live listing" in note
        assert tts.EDGE_VOICES_SNAPSHOT_DATE in note
        assert "GET /api/voices" in note

    def test_offline_status_does_not_claim_the_retired_count(self):
        # The stale catalogue had 53 entries and was the reason the UI looked confident.
        assert tts.get_tts_status()["voices"] != 53

    def test_live_status_reports_verified_confidence(self, fake_live):
        status = tts.get_tts_status()
        assert status["voice_source"] == "live"
        assert status["voice_catalogue_verified"] is True
        assert status["voices"] == len(_live_ids())
        assert "voice_catalogue_note" not in status
        assert "voice_catalogue_snapshot_date" not in status

    def test_status_keeps_the_keys_the_api_layer_relies_on(self):
        status = tts.get_tts_status()
        assert set(status["providers"]) == set(tts.SUPPORTED_PROVIDERS)
        assert status["default_voice"] == tts.DEFAULT_VOICE
        assert status["default_provider"] == "edge"
        assert isinstance(status["voices"], int)
        assert status["ai_gateway"] == tts.get_provider_status()

    def test_availability_is_independent_of_the_catalogue(self, fake_live):
        assert tts.is_available("edge") is True


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
        # Well-formed but not in the catalogue: its own error, and no network at all.
        with mock.patch("httpx.post") as post:
            with pytest.raises(AuthError) as excinfo:
                await tts.synthesize_speech("ola", tmp_path / "n.mp3", "xx-YY-ZzzNeural")
        assert excinfo.value.error_code == TTS_UNKNOWN_VOICE
        post.assert_not_called()
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
