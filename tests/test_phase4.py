"""Phase 4 tests: caption styles, regeneration options, clip metrics and analytics, and batches."""
from conftest import wait_for
from clipper.media import build_captions_ass


def _upload(client, path, **data):
    with open(path, "rb") as fh:
        return client.post("/api/projects", files={"file": ("sample.mp4", fh, "video/mp4")}, data=data)


def _generate_settings(**overrides):
    base = {"theme": "Funny", "clip_length": 8, "clip_count": 2, "captions_style": "classic",
            "orientation": "landscape", "resolution": "720p", "mirror": False}
    base.update(overrides)
    return base


# -- captions -------------------------------------------------------------------------

def test_karaoke_captions_tag_each_word_and_keep_escaping():
    words = [{"start": 0.0, "end": 0.5, "word": " hello"}, {"start": 0.5, "end": 1.2, "word": " {big}"},
             {"start": 1.2, "end": 2.0, "word": " world"}]
    ass = build_captions_ass([{"start": 0, "end": 2, "text": "x"}], words, 0, 5, 1280, 720, "karaoke")
    event = ass.split("[Events]")[1].strip().splitlines()[-1]
    assert "{\\k50}hello" in event and "{\\k70}" in event and "{\\k80}" in event
    assert "(big)" in event  # braces typed by a speaker are never treated as tags
    assert "Dialogue: 0,0:00:00.00,0:00:02.00" in event


def test_karaoke_without_word_timings_falls_back_to_plain_lines():
    ass = build_captions_ass([{"start": 0, "end": 2, "text": "plain line"}], None, 0, 5, 1280, 720, "karaoke")
    assert "\\k" not in ass and "plain line" in ass


def test_unknown_caption_style_falls_back_to_classic():
    ass = build_captions_ass([{"start": 0, "end": 2, "text": "hi"}], None, 0, 5, 1280, 720, "nope")
    assert "Style: Default,Arial" in ass


# -- metrics and analytics ---------------------------------------------------------------

def test_metrics_and_analytics_summary(client, sample_video):
    pid = _upload(client, sample_video).json()["id"]
    wait_for(client, f"/api/projects/{pid}", lambda p: p["status"] == "awaiting_settings")
    client.post(f"/api/projects/{pid}/generate", json=_generate_settings())
    done = wait_for(client, f"/api/projects/{pid}", lambda p: p["status"] in ("done", "failed"))
    first, second = done["clips"][0], done["clips"][1]

    assert client.post(f"/api/clips/{first['id']}/metrics", json={"views": -5}).status_code == 422
    assert client.post(f"/api/clips/{first['id']}/metrics", json={"views": 1000, "likes": 50}).status_code == 200
    assert client.post(f"/api/clips/{second['id']}/metrics", json={"views": 200}).status_code == 200

    analytics = client.get("/api/analytics").json()
    assert analytics["clips_total"] == 2 and analytics["clips_with_metrics"] == 2
    assert analytics["overall"]["avg_views"] == 600.0
    assert analytics["top_clips"][0]["views"] == 1000
    assert analytics["by_theme"]["Funny"]["clips"] == 2
    assert analytics["by_length"]["30 s or less"]["clips"] == 2
    total_by_effects = analytics["with_effects"]["clips"] + analytics["without_effects"]["clips"]
    assert total_by_effects == 2


def test_analytics_empty_before_any_metrics(client):
    data = client.get("/api/analytics").json()
    assert data["clips_total"] == 0 and data["overall"]["avg_views"] is None


# -- batches ---------------------------------------------------------------------------------

def test_batch_settings_saved_first_generate_every_upload_automatically(client, sample_video):
    batch = client.post("/api/batches", json={"name": "Week 12 episodes"}).json()
    bid = batch["id"]
    saved = client.put(f"/api/batches/{bid}/settings", json=_generate_settings(captions_style="karaoke"))
    assert saved.status_code == 200 and saved.json()["applied"] == 0

    pids = [_upload(client, sample_video, batch_id=bid, title=f"Episode {n}").json()["id"] for n in (1, 2)]
    for pid in pids:
        done = wait_for(client, f"/api/projects/{pid}", lambda p: p["status"] in ("done", "failed"))
        assert done["status"] == "done", done
        assert done["batch_id"] == bid and done["captions_style"] == "karaoke"
        assert len(done["clips"]) == 2

    view = client.get(f"/api/batches/{bid}").json()
    assert view["status_counts"] == {"done": 2}
    assert [item["title"] for item in view["items"]] == ["Episode 1", "Episode 2"]


def test_batch_settings_applied_to_projects_already_transcribed(client, sample_video):
    bid = client.post("/api/batches", json={"name": "Late settings"}).json()["id"]
    pid = _upload(client, sample_video, batch_id=bid).json()["id"]
    wait_for(client, f"/api/projects/{pid}", lambda p: p["status"] == "awaiting_settings")
    result = client.put(f"/api/batches/{bid}/settings", json=_generate_settings(clip_count=1)).json()
    assert result["applied"] == 1 and result["errors"] == []
    done = wait_for(client, f"/api/projects/{pid}", lambda p: p["status"] in ("done", "failed"))
    assert done["status"] == "done" and len(done["clips"]) == 1


def test_batch_reports_projects_whose_length_does_not_fit(client, sample_video):
    bid = client.post("/api/batches", json={"name": "Too long"}).json()["id"]
    pid = _upload(client, sample_video, batch_id=bid).json()["id"]
    wait_for(client, f"/api/projects/{pid}", lambda p: p["status"] == "awaiting_settings")
    result = client.put(f"/api/batches/{bid}/settings", json=_generate_settings(clip_length=600)).json()
    assert result["applied"] == 0 and len(result["errors"]) == 1
    assert "cannot exceed" in result["errors"][0]


def test_deleting_a_batch_keeps_its_projects(client, sample_video):
    bid = client.post("/api/batches", json={"name": "Temporary"}).json()["id"]
    pid = _upload(client, sample_video, batch_id=bid).json()["id"]
    assert client.delete(f"/api/batches/{bid}").json()["deleted"] is True
    assert client.get(f"/api/batches/{bid}").status_code == 404
    project = client.get(f"/api/projects/{pid}").json()
    assert project["batch_id"] is None


def test_upload_into_someone_elses_batch_is_refused(client, sample_video):
    res = _upload(client, sample_video, batch_id="not-a-real-batch")
    assert res.status_code == 404


# -- regeneration options ------------------------------------------------------------------

def test_regenerate_accepts_new_caption_style_and_rejects_unknown(client, sample_video):
    pid = _upload(client, sample_video).json()["id"]
    wait_for(client, f"/api/projects/{pid}", lambda p: p["status"] == "awaiting_settings")
    client.post(f"/api/projects/{pid}/generate", json=_generate_settings(clip_count=1))
    done = wait_for(client, f"/api/projects/{pid}", lambda p: p["status"] in ("done", "failed"))
    clip_id = done["clips"][0]["id"]
    assert client.post(f"/api/clips/{clip_id}/regenerate", json={"captions_style": "wavy"}).status_code == 400
    assert client.post(f"/api/clips/{clip_id}/regenerate", json={"captions_style": "off", "mirror": True}).status_code == 202
    again = wait_for(client, f"/api/projects/{pid}",
                     lambda p: p["status"] == "done" and p["clips"][0]["settings"]["captions_style"] == "off")
    assert again["clips"][0]["settings"]["mirror"] is True
