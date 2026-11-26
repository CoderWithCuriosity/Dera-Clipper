"""YouTube links through yt-dlp. Off by default; tests cover the off state, the on state
with a stubbed download, and the link rules."""
import shutil

import pytest

from conftest import FakeEngine, wait_for
from clipper.security import is_platform_link


def test_platform_link_detection():
    assert is_platform_link("https://www.youtube.com/watch?v=jNQXAC9IVRw")
    assert is_platform_link("https://youtu.be/jNQXAC9IVRw")
    assert not is_platform_link("https://example.com/video.mp4")
    assert not is_platform_link("https://youtube.com.evil.example/video")


def test_youtube_link_refused_when_off(client):
    res = client.post("/api/projects", data={"url": "https://www.youtube.com/watch?v=jNQXAC9IVRw"})
    assert res.status_code == 400 and "turned off" in res.json()["detail"]
    assert client.get("/api/health").json()["platform_links"] is False


def test_youtube_link_flow_when_enabled(app_env, monkeypatch, sample_video):
    from fastapi.testclient import TestClient

    import clipper.api as api_module
    import clipper.pipeline as pipeline
    from clipper.api import create_app
    from clipper.config import load_settings

    monkeypatch.setenv("CLIPPER_ALLOW_PLATFORM_LINKS", "true")
    monkeypatch.setattr(pipeline, "build_transcription_chain",
                        lambda svc, user_id, mode: [("local", FakeEngine(duration=40.0))])
    monkeypatch.setattr(api_module, "validate_link", lambda url: url)  # skip the DNS check in tests

    def fake_download(url, work, ffmpeg_location, max_bytes, cookies_file=None):
        dst = work / "platform.mp4"
        shutil.copyfile(sample_video, dst)
        return dst

    monkeypatch.setattr(pipeline, "download_platform_link", fake_download)
    with TestClient(create_app(load_settings())) as c:
        assert c.get("/api/health").json()["platform_links"] is True
        res = c.post("/api/projects", data={"url": "https://www.youtube.com/watch?v=jNQXAC9IVRw"})
        assert res.status_code == 201, res.text
        pid = res.json()["id"]
        project = wait_for(c, f"/api/projects/{pid}", lambda p: p["status"] in ("awaiting_settings", "failed"))
        assert project["status"] == "awaiting_settings", project
        assert 39 < project["duration"] < 41


def test_real_download_is_not_run_in_the_default_suite():
    """Placeholder so the suite documents that no live YouTube request is made by default."""
    pytest.importorskip("yt_dlp")
