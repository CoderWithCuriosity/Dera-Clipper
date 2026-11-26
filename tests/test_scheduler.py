"""The job queue runs heavy steps (transcribing, rendering) one or a few at a time, and light
steps (planning, downloads) alongside them, so the server never has to run everything at once."""
import pytest

from clipper import services
from clipper.config import load_settings
from clipper.services import Services


@pytest.fixture()
def svc(app_env, monkeypatch):
    monkeypatch.setenv("CLIPPER_HEAVY_JOBS", "1")
    monkeypatch.setenv("CLIPPER_LIGHT_JOBS", "2")
    return Services.create(load_settings())


def _enqueue(svc, job_type):
    return svc.queue.enqueue("local", None, job_type)


def test_only_one_heavy_job_runs_while_light_jobs_keep_moving(svc, monkeypatch):
    monkeypatch.setattr(services, "memory_has_room", lambda: True)
    _enqueue(svc, "transcribe")
    _enqueue(svc, "transcribe")
    _enqueue(svc, "plan")
    claims = [svc.queue.claim() for _ in range(3)]
    assert sorted(j["type"] for j in claims if j) == ["plan", "transcribe"]
    assert claims.count(None) == 1  # the second transcription waits for the heavy slot


def test_a_finished_heavy_job_frees_its_slot(svc, monkeypatch):
    monkeypatch.setattr(services, "memory_has_room", lambda: True)
    _enqueue(svc, "transcribe")
    _enqueue(svc, "transcribe")
    first = svc.queue.claim()
    assert first["type"] == "transcribe" and svc.queue.claim() is None
    svc.queue.finish(first["id"])
    second = svc.queue.claim()
    assert second is not None and second["type"] == "transcribe"


def test_heavy_jobs_wait_when_memory_is_short(svc, monkeypatch):
    monkeypatch.setattr(services, "memory_has_room", lambda: False)
    _enqueue(svc, "render")
    _enqueue(svc, "plan")
    job = svc.queue.claim()
    assert job["type"] == "plan"  # light work still runs
    assert svc.queue.claim() is None  # the render waits for memory


def test_queue_status_reports_busy_server(svc, monkeypatch):
    monkeypatch.setattr(services, "memory_has_room", lambda: True)
    _enqueue(svc, "transcribe")
    _enqueue(svc, "transcribe")
    svc.queue.claim()
    status = services.server_load(svc)
    assert status["heavy_running"] == 1 and status["heavy_limit"] == 1 and status["waiting"] == 1
