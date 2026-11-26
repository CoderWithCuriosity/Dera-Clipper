"""The 'no effects' option, and B-roll that fills the frame with captions kept on top."""
import subprocess
import re

from conftest import wait_for


def test_clips_have_no_effects_when_turned_off(client, sample_video):
    with open(sample_video, "rb") as fh:
        pid = client.post("/api/projects", files={"file": ("s.mp4", fh, "video/mp4")}).json()["id"]
    wait_for(client, f"/api/projects/{pid}", lambda p: p["status"] == "awaiting_settings")
    res = client.post(f"/api/projects/{pid}/generate", json={
        "theme": "Funny", "clip_length": 8, "clip_count": 2, "effects": False, "resolution": "720p",
    })
    assert res.status_code == 202, res.text
    done = wait_for(client, f"/api/projects/{pid}", lambda p: p["status"] in ("done", "failed"))
    assert done["status"] == "done", done
    assert done["effects_enabled"] is False
    assert all(c["effects"] == [] for c in done["clips"])


def test_effects_are_used_by_default(client, sample_video):
    with open(sample_video, "rb") as fh:
        pid = client.post("/api/projects", files={"file": ("s.mp4", fh, "video/mp4")}).json()["id"]
    wait_for(client, f"/api/projects/{pid}", lambda p: p["status"] == "awaiting_settings")
    client.post(f"/api/projects/{pid}/generate", json={"theme": "Funny", "clip_length": 8, "clip_count": 2})
    done = wait_for(client, f"/api/projects/{pid}", lambda p: p["status"] in ("done", "failed"))
    assert done["effects_enabled"] is True
    assert any(c["effects"] for c in done["clips"])


def test_full_frame_broll_with_captions_renders(ffmpeg_bin, sample_video, tmp_path):
    from clipper import media

    clip = tmp_path / "red.mp4"
    subprocess.run([ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", "color=c=red:size=320x240:duration=3", "-pix_fmt", "yuv420p", str(clip)], check=True)
    ass = media.build_captions_ass([{"start": 0, "end": 6, "text": "caption stays"}], None, 0, 6, 1280, 720, "classic")
    ff = media.Ffmpeg(ffmpeg_bin)
    out = ff.render_clip(sample_video, tmp_path / "full.mp4", 0.0, 6.0, work_dir=tmp_path / "w",
                         resolution="720p", captions=ass, effects=[media.Effect(path=clip, kind="broll", at=1.0, duration=2.0)])
    info = subprocess.run([ffmpeg_bin, "-hide_banner", "-i", str(out)], capture_output=True, text=True).stderr
    assert re.search(r"1280x720", info)
    assert abs(ff.probe(out)["duration"] - 6.0) < 0.6
