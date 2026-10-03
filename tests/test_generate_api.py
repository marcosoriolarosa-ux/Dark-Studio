"""API tests for the one-click generation endpoints.

No network, no real TTS, no real render: every case that submits a job replaces
``generator.run_generation`` with a stub, because that is the one function that
would reach a provider or a headless browser. The registry is module-level
state, so it is emptied around every test - a job submitted by one case would
otherwise still be listed by the next one.

The interesting contract is the tolerance of POST /api/generate: the frontend
module that calls it is already built, but an older or hand-rolled client can
send keys this backend does not know, and those must be dropped rather than
turned into a 400 or a 500.
"""
import asyncio
from dataclasses import fields as dataclass_fields
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from backend import app as app_module
from backend.services import generator
from backend.services import script_gen

client = TestClient(app_module.app)

BLANK_TOPIC_DETAIL = "Indique um tema para gerar o video."
JOB_NOT_FOUND = "Job não encontrado."
JOB_KEYS = {
    "job_id", "status", "progress", "stage", "message", "params", "result",
    "error", "created_at", "updated_at", "_seq",
}


@pytest.fixture(autouse=True)
def clean_registry():
    """Empty the job registry before and after every test.

    Job ids and the concurrency semaphore are module-level, so leaking either one
    between cases makes list_jobs() and the cancellation tests order-dependent.
    """
    generator.reset_for_tests()
    yield
    generator.reset_for_tests()


@pytest.fixture
def stub_pipeline():
    """A pipeline that returns instantly and never leaves the process.

    The stub never touches the job, which leaves it QUEUED: that is what makes the
    cancellation test deterministic, since a job that never starts is exactly the
    one cancel_job is allowed to stop.
    """
    async def _stub(job):
        return {}

    with mock.patch.object(generator, "run_generation", _stub):
        yield


def _submit(**body):
    return client.post("/api/generate", json=body)


def _queued_id(**body):
    """Submit a job and return its id, asserting the happy path on the way."""
    response = _submit(**body)
    assert response.status_code == 202, response.text
    return response.json()["job_id"]


class TestSubmitGeneration:
    def test_valid_topic_is_202_with_a_queued_job(self, stub_pipeline):
        response = _submit(topic="o custo invisível do dinheiro")
        assert response.status_code == 202
        body = response.json()
        assert set(body) == JOB_KEYS
        assert body["status"] == "queued"
        assert body["job_id"]
        assert body["progress"] == 0.0
        assert body["error"] is None
        assert body["result"] is None
        assert body["params"]["topic"] == "o custo invisível do dinheiro"

    def test_every_optional_field_is_accepted(self, stub_pipeline):
        """The documented body in full: nothing in it may be rejected."""
        response = _submit(
            topic="energia",
            language="pt-PT",
            voice="pt-PT-RaquelNeural",
            tts_provider="edge",
            rate="+0%",
            section_count=5,
            tone="documentary",
            duration_target=60,
            custom_instructions="",
            aspect_ratio="vertical",
            preset="cinematic",
            subtitle_style=None,
            music_track=None,
            music_mood="ambient",
            music_volume=0.18,
            duck_voice=True,
            include_captions=True,
            project_prefix="",
        )
        assert response.status_code == 202
        params = response.json()["params"]
        assert params["music_mood"] == "ambient"
        assert params["music_volume"] == 0.18
        assert params["aspect_ratio"] == "vertical"
        assert params["section_count"] == 5

    @pytest.mark.parametrize("body", [
        {},
        {"topic": ""},
        {"topic": "   "},
        {"language": "pt-PT", "section_count": 3},
    ])
    def test_blank_or_missing_topic_is_a_400_with_the_exact_message(
        self, stub_pipeline, body
    ):
        response = client.post("/api/generate", json=body)
        assert response.status_code == 400
        assert response.json()["detail"] == BLANK_TOPIC_DETAIL

    def test_unknown_keys_are_ignored_instead_of_rejected(self, stub_pipeline):
        """A client built against another version of the form must still work.

        submit_job does GenerationRequest(**params), so mood/project_name would
        otherwise come back as a TypeError wrapped in a 400.
        """
        response = client.post("/api/generate", json={
            "topic": "energia",
            "mood": "dark",
            "project_name": "x",
            "section_count": 3,
            "not_a_field_at_all": [1, 2, 3],
        })
        assert response.status_code == 202
        params = response.json()["params"]
        # The declared keys survive...
        assert params["topic"] == "energia"
        assert params["section_count"] == 3
        # ...and the undeclared ones never reach the job the poller reads.
        assert "mood" not in params
        assert "project_name" not in params
        assert "not_a_field_at_all" not in params

    def test_the_filter_is_derived_from_the_dataclass(self):
        """Guards the drift: the allowed set is read off GenerationRequest."""
        declared = {f.name for f in dataclass_fields(generator.GenerationRequest)}
        assert app_module.GENERATION_PARAM_FIELDS == declared
        assert app_module._generation_params({
            "topic": "energia", "mood": "dark", "whatever": 1
        }) == {"topic": "energia"}
        # A body that is not a mapping degrades to the dataclass defaults,
        # which then fails validation as a blank topic instead of a 500.
        assert app_module._generation_params(["energia"]) == {}

    @pytest.mark.parametrize("overrides", [
        {"section_count": script_gen.MAX_SECTIONS + 1},
        {"section_count": 0},
        {"section_count": "muitos"},
        {"music_volume": 5.0},
        {"music_volume": -0.5},
        {"aspect_ratio": "diagonal"},
        {"preset": "vaporwave"},
        {"language": "kl-KL"},
        {"duration_target": 0},
    ])
    def test_out_of_range_values_are_a_400_not_a_500(self, stub_pipeline, overrides):
        response = _submit(topic="energia", **overrides)
        assert response.status_code == 400
        assert response.json()["detail"]

    def test_unexpected_failure_is_a_500_with_a_portuguese_detail(self, stub_pipeline):
        with mock.patch.object(generator, "submit_job", side_effect=MemoryError("boom")):
            response = _submit(topic="energia")
        assert response.status_code == 500
        detail = response.json()["detail"]
        assert "Falha ao enfileirar a geração" in detail

    def test_post_returns_before_the_pipeline_finishes(self):
        """The route queues the job; it must never await the pipeline.

        TestClient runs every request on a throwaway event loop and tears it down
        once the reply is out, so a coroutine that is still sleeping when the
        response arrives gets cancelled right after - which is the proof that the
        POST did not wait for it.
        """
        state = {"started": False, "finished": False, "cancelled": False}

        async def _slow(job):
            state["started"] = True
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                state["cancelled"] = True
                raise
            state["finished"] = True
            return {}

        with mock.patch.object(generator, "run_generation", _slow):
            response = _submit(topic="energia")

        assert response.status_code == 202
        assert state["started"] is True
        assert state["finished"] is False
        assert state["cancelled"] is True


class TestJobRegistry:
    def test_list_contains_the_submitted_job_newest_first(self, stub_pipeline):
        first = _queued_id(topic="primeiro")
        second = _queued_id(topic="segundo")
        response = client.get("/api/jobs")
        assert response.status_code == 200
        body = response.json()
        assert isinstance(body, list)
        ids = [job["job_id"] for job in body]
        assert ids == [second, first]
        assert {job["params"]["topic"] for job in body} == {"primeiro", "segundo"}

    def test_list_is_empty_before_anything_is_submitted(self):
        assert client.get("/api/jobs").json() == []

    def test_get_job_returns_the_snapshot_the_post_returned(self, stub_pipeline):
        created = _submit(topic="energia").json()
        response = client.get("/api/jobs/" + created["job_id"])
        assert response.status_code == 200
        assert response.json() == created

    def test_unknown_job_is_a_404(self):
        response = client.get("/api/jobs/nao-existe")
        assert response.status_code == 404
        assert response.json()["detail"] == JOB_NOT_FOUND

    def test_delete_cancels_a_queued_job(self, stub_pipeline):
        job_id = _queued_id(topic="energia")
        assert generator.get_job(job_id).status == "queued"
        response = client.delete("/api/jobs/" + job_id)
        assert response.status_code == 200
        assert response.json() == {"cancelled": True}
        assert generator.get_job(job_id).status == "cancelled"

    def test_cancelling_twice_reports_false_instead_of_404(self, stub_pipeline):
        job_id = _queued_id(topic="energia")
        assert client.delete("/api/jobs/" + job_id).json() == {"cancelled": True}
        response = client.delete("/api/jobs/" + job_id)
        assert response.status_code == 200
        assert response.json() == {"cancelled": False}

    def test_delete_on_an_unknown_job_is_a_404(self):
        response = client.delete("/api/jobs/nao-existe")
        assert response.status_code == 404
        assert response.json()["detail"] == JOB_NOT_FOUND

    def test_head_probe_reports_405_because_the_route_is_mounted(self):
        # The frontend probes with HEAD and reads 405 as "mounted" and 404 as
        # "absent"; only POST is declared, so Starlette answers 405.
        assert client.head("/api/generate").status_code == 405
