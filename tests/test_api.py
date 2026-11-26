"""End-to-end API tests. Real FFmpeg renders the clips; transcription uses a fake engine."""
import io
import zipfile

from conftest import wait_for


def _upload(client, path, title="Test video"):
    with open(path, "rb") as fh:
        return client.post("/api/projects", files={"file": ("sample.mp4", fh, "video/mp4")},
                           data={"title": title})


def test_full_flow_upload_transcribe_generate_download(client, sample_video):
    res = _upload(client, sample_video)
    assert res.status_code == 201, res.text
    pid = res.json()["id"]

    project = wait_for(client, f"/api/projects/{pid}", lambda p: p["status"] in ("awaiting_settings", "failed"))
    assert project["status"] == "awaiting_settings", project
    assert 39 < project["duration"] < 41
    assert project["has_transcript"] is True

    transcript = client.get(f"/api/projects/{pid}/transcript").json()
    assert transcript["engine"] == "fake" and len(transcript["segments"]) >= 9

    res = client.post(f"/api/projects/{pid}/generate", json={
        "theme": "Funny", "clip_length": 10, "clip_count": 2, "mirror": True,
        "captions_style": "bold", "orientation": "vertical", "resolution": "720p",
    })
    assert res.status_code == 202, res.text

    done = wait_for(client, f"/api/projects/{pid}", lambda p: p["status"] in ("done", "failed"))
    assert done["status"] == "done", done
    clips = done["clips"]
    assert len(clips) == 2
    assert all(c["status"] == "done" for c in clips), clips
    assert all(9 <= c["length"] <= 12 for c in clips)
    assert all(c["reason"] for c in clips)
    assert clips[0]["settings"]["orientation"] == "vertical"

    video = client.get(clips[0]["video_url"])
    assert video.status_code == 200
    assert video.headers["content-type"].startswith("video/mp4")
    assert b"ftyp" in video.content[:32]

    download = client.get(clips[0]["download_url"])
    assert "attachment" in download.headers.get("content-disposition", "")

    archive = client.get(f"/api/projects/{pid}/zip")
    assert archive.status_code == 200
    names = zipfile.ZipFile(io.BytesIO(archive.content)).namelist()
    assert len(names) == 2 and all(n.endswith(".mp4") for n in names)


def test_regenerate_one_clip_with_new_orientation(client, sample_video):
    pid = _upload(client, sample_video).json()["id"]
    wait_for(client, f"/api/projects/{pid}", lambda p: p["status"] == "awaiting_settings")
    client.post(f"/api/projects/{pid}/generate", json={
        "theme": "Funny", "clip_length": 8, "clip_count": 1,
    })
    done = wait_for(client, f"/api/projects/{pid}", lambda p: p["status"] in ("done", "failed"))
    clip_id = done["clips"][0]["id"]

    res = client.post(f"/api/clips/{clip_id}/regenerate", json={"orientation": "square"})
    assert res.status_code == 202, res.text
    again = wait_for(client, f"/api/projects/{pid}",
                     lambda p: p["status"] == "done" and p["clips"][0]["settings"]["orientation"] == "square")
    assert again["clips"][0]["status"] == "done"


def test_validation_rules(client, sample_video):
    pid = _upload(client, sample_video).json()["id"]
    wait_for(client, f"/api/projects/{pid}", lambda p: p["status"] == "awaiting_settings")

    too_long = client.post(f"/api/projects/{pid}/generate", json={
        "theme": "Funny", "clip_length": 999, "clip_count": 2})
    assert too_long.status_code == 400
    assert client.post(f"/api/projects/{pid}/generate", json={
        "theme": "Funny", "clip_length": 0, "clip_count": 2}).status_code == 422
    assert client.post(f"/api/projects/{pid}/generate", json={
        "theme": "Funny", "clip_length": 10, "clip_count": 25}).status_code == 422
    assert client.post(f"/api/projects/{pid}/generate", json={
        "theme": "Nope", "clip_length": 10, "clip_count": 2}).status_code == 400
    assert client.post(f"/api/projects/{pid}/generate", json={
        "theme": "Funny", "clip_length": 10, "clip_count": 2, "orientation": "diagonal"}).status_code == 400


def test_bad_uploads_and_links_are_rejected(client, tmp_path):
    fake = tmp_path / "fake.mp4"
    fake.write_bytes(b"plain text pretending to be a video")
    with open(fake, "rb") as fh:
        res = client.post("/api/projects", files={"file": ("fake.mp4", fh, "video/mp4")})
    assert res.status_code == 400

    res = client.post("/api/projects", files={"file": ("notes.txt", b"hi", "text/plain")})
    assert res.status_code == 400

    res = client.post("/api/projects", data={"url": "http://127.0.0.1:8000/secret.mp4"})
    assert res.status_code == 400

    res = client.post("/api/projects", data={})
    assert res.status_code == 400


def test_delete_project_removes_it(client, sample_video):
    pid = _upload(client, sample_video).json()["id"]
    wait_for(client, f"/api/projects/{pid}", lambda p: p["status"] == "awaiting_settings")
    assert client.delete(f"/api/projects/{pid}").status_code == 200
    assert client.get(f"/api/projects/{pid}").status_code == 404


def test_provider_crud_hides_keys_and_requires_key_where_needed(client):
    assert client.post("/api/providers", json={"preset": "groq", "priority": 1}).status_code == 400

    res = client.post("/api/providers", json={"preset": "groq", "api_key": "gsk_abcdef1234", "priority": 1, "verify": False})
    assert res.status_code == 201
    created = res.json()
    assert "gsk_" not in res.text and created["key_hint"].endswith("1234")

    res = client.post("/api/providers", json={"preset": "ollama", "priority": 2, "verify": False})
    assert res.status_code == 201

    listing = client.get("/api/providers").json()["providers"]
    assert [p["priority"] for p in listing] == [1, 2]

    patched = client.patch(f"/api/providers/{created['id']}", json={"enabled": False}).json()
    assert patched["enabled"] is False

    assert client.delete(f"/api/providers/{created['id']}").status_code == 200
    assert len(client.get("/api/providers").json()["providers"]) == 1


def test_settings_validation_and_defaults(client):
    values = client.get("/api/settings").json()["values"]
    assert values["default_clip_count"] == 5
    assert client.put("/api/settings", json={"values": {"default_clip_count": 0}}).status_code == 400
    assert client.put("/api/settings", json={"values": {"unknown_key": 1}}).status_code == 400
    ok = client.put("/api/settings", json={"values": {"default_orientation": "vertical"}})
    assert ok.status_code == 200 and ok.json()["values"]["default_orientation"] == "vertical"


def test_preflight_reports_ffmpeg(client):
    checks = client.get("/api/preflight").json()["checks"]
    ffmpeg = [c for c in checks if c["name"] == "FFmpeg"][0]
    assert ffmpeg["ok"] is True


def test_provider_that_fails_its_test_is_not_saved(client):
    res = client.post("/api/providers", json={"preset": "custom", "base_url": "http://127.0.0.1:9/v1",
                                              "model": "m", "priority": 5})
    assert res.status_code == 400 and "not saved" in res.json()["detail"]
    assert all(p["priority"] != 5 for p in client.get("/api/providers").json()["providers"])
