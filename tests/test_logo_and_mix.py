"""Brand logo and mixed highlight clips. Real FFmpeg renders are measured by reading pixels
and durations back out of the finished files."""
import subprocess

import pytest

from conftest import wait_for
from clipper import media
from clipper.branding import check_logo_file, clean_logo
from clipper.copy import heuristic_copy, load_copies, write_copy
from clipper.planner import Moment, draw_sizes, group_highlights


def _run(ffmpeg_bin, *args) -> bytes:
    proc = subprocess.run([ffmpeg_bin, "-hide_banner", "-loglevel", "error", *args], capture_output=True)
    assert proc.returncode == 0, proc.stderr.decode(errors="replace")
    return proc.stdout


@pytest.fixture()
def red_logo(tmp_path, ffmpeg_bin):
    path = tmp_path / "red.png"
    _run(ffmpeg_bin, "-y", "-f", "lavfi", "-i", "color=c=red:s=200x200", "-frames:v", "1", str(path))
    return path


@pytest.fixture()
def black_source(tmp_path, ffmpeg_bin):
    """A plain black video, so a logo's colour can be measured exactly."""
    path = tmp_path / "black.mp4"
    _run(ffmpeg_bin, "-y", "-f", "lavfi", "-i", "color=c=black:s=640x360:r=25:d=4",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(path))
    return path


def _pixel(ffmpeg_bin, path, x, y):
    """RGB of the pixel at (x, y). A 2x2 block is read because yuv420p needs even crop sizes."""
    raw = _run(ffmpeg_bin, "-i", str(path), "-vf", f"crop=2:2:{x}:{y}", "-frames:v", "1",
               "-f", "rawvideo", "-pix_fmt", "rgb24", "-")
    return tuple(raw[:3])


def _moment(start, end, score):
    return Moment(start=start, end=end, score=score, reason=f"moment {start}")


# -- rules ----------------------------------------------------------------------------

def test_logo_options_are_clamped_and_default_to_off():
    assert clean_logo(None)["enabled"] is False
    cleaned = clean_logo({"enabled": True, "opacity": 500, "size": 0, "x": -4, "y": "50"})
    assert cleaned == {"enabled": True, "opacity": 100.0, "size": 5.0, "x": 0.0, "y": 50.0}


def test_logo_upload_must_be_a_real_image():
    assert check_logo_file("brand.PNG", b"\x89PNG\r\n\x1a\n") == ".png"
    with pytest.raises(ValueError):
        check_logo_file("notes.txt", b"\x89PNG")
    with pytest.raises(ValueError):
        check_logo_file("fake.png", b"hello, not an image")


def test_group_highlights_deals_ranked_moments_and_keeps_video_order():
    moments = [_moment(0, 5, 0.9), _moment(10, 15, 0.8), _moment(20, 25, 0.7),
               _moment(30, 35, 0.6), _moment(40, 45, 0.5), _moment(50, 55, 0.4)]
    groups = group_highlights(moments, [3, 3])
    assert len(groups) == 2 and all(len(g) == 3 for g in groups)
    # Each clip gets a mix: the two best moments land in different clips.
    assert {m.start for g in groups for m in g[:1]} == {0, 10}
    for group in groups:
        starts = [m.start for m in group]
        assert starts == sorted(starts)


def test_group_highlights_makes_fewer_clips_when_moments_run_short():
    moments = [_moment(0, 5, 0.9), _moment(10, 15, 0.8), _moment(20, 25, 0.7), _moment(30, 35, 0.6)]
    groups = group_highlights(moments, [3, 3, 3, 3, 3])
    assert len(groups) == 1 and len(groups[0]) == 3  # one clip from the best three moments
    assert group_highlights([], [3, 3]) == []


def test_mixed_captions_are_moved_onto_the_joined_timeline():
    segments = [
        {"start": 10.0, "end": 12.0, "text": "first moment"},
        {"start": 30.0, "end": 32.0, "text": "second moment"},
    ]
    ass = media.build_mix_captions_ass(segments, None, [(10.0, 15.0), (30.0, 35.0)], 1280, 720, "classic")
    events = [line for line in ass.splitlines() if line.startswith("Dialogue")]
    assert "first moment" in events[0] and "0:00:00.00" in events[0]
    # The second moment starts after the first 5 seconds of the joined clip.
    assert "second moment" in events[1] and "0:00:05.00" in events[1]


# -- rendering -------------------------------------------------------------------------

def test_logo_sits_where_it_is_placed(ffmpeg_bin, black_source, red_logo, tmp_path):
    ff = media.Ffmpeg(ffmpeg_bin)
    top_left = ff.render_clip(black_source, tmp_path / "tl.mp4", 0, 2, work_dir=tmp_path / "w1",
                              resolution="720p", has_audio=False,
                              logo=media.Logo(path=red_logo, opacity=100, size=20, x=0, y=0))
    r, g, b = _pixel(ffmpeg_bin, top_left, 10, 10)
    assert r > 230 and g < 40 and b < 40, (r, g, b)
    assert sum(_pixel(ffmpeg_bin, top_left, 1270, 710)) < 60  # far corner stays black

    bottom_right = ff.render_clip(black_source, tmp_path / "br.mp4", 0, 2, work_dir=tmp_path / "w2",
                                  resolution="720p", has_audio=False,
                                  logo=media.Logo(path=red_logo, opacity=100, size=20, x=100, y=100))
    r, g, b = _pixel(ffmpeg_bin, bottom_right, 1270, 710)
    assert r > 230 and g < 40, (r, g, b)
    assert sum(_pixel(ffmpeg_bin, bottom_right, 10, 10)) < 60


def test_logo_opacity_fades_the_logo(ffmpeg_bin, black_source, red_logo, tmp_path):
    ff = media.Ffmpeg(ffmpeg_bin)
    out = ff.render_clip(black_source, tmp_path / "half.mp4", 0, 2, work_dir=tmp_path / "w",
                         resolution="720p", has_audio=False,
                         logo=media.Logo(path=red_logo, opacity=50, size=20, x=0, y=0))
    r, g, _ = _pixel(ffmpeg_bin, out, 10, 10)
    assert 100 < r < 160 and g < 30, (r, g)  # about half of full red over black


def test_mixed_clip_joins_its_moments(ffmpeg_bin, sample_video, tmp_path):
    ff = media.Ffmpeg(ffmpeg_bin)
    ranges = [(2.0, 6.0), (20.0, 24.0), (30.0, 33.0)]
    segments = [{"start": 2.0, "end": 6.0, "text": "one"}, {"start": 20.0, "end": 24.0, "text": "two"},
                {"start": 30.0, "end": 33.0, "text": "three"}]
    captions = media.build_mix_captions_ass(segments, None, ranges, 1280, 720, "classic")
    out = ff.render_mix(sample_video, tmp_path / "mix.mp4", ranges, work_dir=tmp_path / "w",
                        resolution="720p", captions=captions)
    info = ff.probe(out)
    assert abs(info["duration"] - 11.0) < 0.5, info
    assert info["has_audio"] is True


# -- API -------------------------------------------------------------------------------

def _png_bytes(ffmpeg_bin, tmp_path) -> bytes:
    path = tmp_path / "api-logo.png"
    _run(ffmpeg_bin, "-y", "-f", "lavfi", "-i", "color=c=blue:s=300x120", "-frames:v", "1", str(path))
    return path.read_bytes()


def _upload_video(client, path):
    with open(path, "rb") as fh:
        res = client.post("/api/projects", files={"file": ("sample.mp4", fh, "video/mp4")},
                          data={"title": "Logo test"})
    assert res.status_code == 201, res.text
    pid = res.json()["id"]
    wait_for(client, f"/api/projects/{pid}", lambda p: p["status"] in ("awaiting_settings", "failed"))
    return pid


def test_logo_upload_settings_and_rejections(client, ffmpeg_bin, tmp_path):
    bad = client.post("/api/logo", files={"file": ("logo.png", b"not an image", "image/png")})
    assert bad.status_code == 400

    png = _png_bytes(ffmpeg_bin, tmp_path)
    res = client.post("/api/logo", files={"file": ("brand.png", png, "image/png")})
    assert res.status_code == 201, res.text
    assert res.json()["logo"]["name"] == "brand.png"

    image = client.get("/api/logo/image")
    assert image.status_code == 200 and image.headers["content-type"] == "image/png"

    saved = client.put("/api/settings", json={"values": {
        "default_logo_on": True, "default_logo_x": 50, "default_logo_y": 3, "default_logo_size": 20,
        "default_moments_per_clip": 3,
    }})
    assert saved.status_code == 200, saved.text
    values = saved.json()["values"]
    assert values["default_logo_on"] is True and values["default_moments_per_clip"] == 3

    assert client.put("/api/settings", json={"values": {"default_logo_size": 99}}).status_code == 400
    assert client.put("/api/settings", json={"values": {"default_moments_per_clip": 9}}).status_code == 400

    assert client.delete("/api/logo").status_code == 200
    assert client.get("/api/logo").json()["logo"] is None


def test_mixed_clips_with_logo_render_end_to_end(client, ffmpeg_bin, sample_video, tmp_path):
    client.post("/api/logo", files={"file": ("brand.png", _png_bytes(ffmpeg_bin, tmp_path), "image/png")})
    pid = _upload_video(client, sample_video)

    res = client.post(f"/api/projects/{pid}/generate", json={
        "theme": "Funny", "clip_length": 15, "clip_count": 2, "moments_per_clip": 3,
        "logo": {"enabled": True, "opacity": 80, "size": 15, "x": 97, "y": 3},
    })
    assert res.status_code == 202, res.text
    assert res.json()["moments_per_clip"] == 3 and res.json()["logo"]["enabled"] is True

    done = wait_for(client, f"/api/projects/{pid}", lambda p: p["status"] in ("done", "failed"))
    assert done["status"] == "done", done
    clips = done["clips"]
    assert 1 <= len(clips) <= 2
    for clip in clips:
        assert clip["status"] == "done", clip
        assert clip["moments"] == 3 and len(clip["segments"]) == 3
        assert 13 <= clip["length"] <= 17
        assert clip["settings"]["logo"]["enabled"] is True
        assert b"ftyp" in client.get(clip["video_url"]).content[:32]


def test_generation_rejects_bad_mix_and_missing_logo(client, sample_video):
    pid = _upload_video(client, sample_video)
    base = {"theme": "Funny", "clip_length": 15, "clip_count": 2}

    assert client.post(f"/api/projects/{pid}/generate", json={**base, "moments_per_clip": 9}).status_code == 422

    too_short = client.post(f"/api/projects/{pid}/generate", json={**base, "clip_length": 6, "moments_per_clip": 3})
    assert too_short.status_code == 400 and "shorter than" in too_short.json()["detail"]

    no_logo = client.post(f"/api/projects/{pid}/generate", json={**base, "logo": {"enabled": True}})
    assert no_logo.status_code == 400 and "Upload your logo" in no_logo.json()["detail"]


def test_random_sizes_stay_in_range_and_zero_is_refused():
    sizes = draw_sizes(2, 4, 50)
    assert len(sizes) == 50 and all(2 <= n <= 4 for n in sizes)
    assert draw_sizes(3, 3, 2) == [3, 3]
    for bad in [(0, 2, 3), (3, 2, 3), (2, 3, 0)]:
        with pytest.raises(ValueError):
            draw_sizes(*bad)


def test_grouping_gives_each_clip_its_own_size_and_never_empty_clips():
    moments = [_moment(i * 10, i * 10 + 5, 1 - i / 10) for i in range(6)]
    groups = group_highlights(moments, [1, 4, 2])  # 7 wanted, 6 found: the last clip is left out
    assert [len(g) for g in groups] == [1, 4]
    assert all(len(g) >= 1 for g in groups)
    with pytest.raises(ValueError):
        group_highlights(moments, [2, 0])


def test_copy_falls_back_to_the_clips_own_words_and_keeps_history():
    text = "This is the funny part. Nobody expected the ending! Then everything broke."
    first = heuristic_copy(text, 0)
    second = heuristic_copy(text, 1)
    assert first["title"].startswith("This is the funny part") and first["source"] == "heuristic"
    assert second["title"].startswith("Nobody expected")
    assert write_copy("", [], 0)["title"] == "Clip"  # an empty transcript never produces an empty title
    data = load_copies('{"versions": [{"title": "A"}, {"title": "B"}], "active": 0}')
    assert [v["title"] for v in data["versions"]] == ["A", "B"] and data["active"] == 0
    assert load_copies(None) == {"versions": [], "active": 0}


def _short_video(ffmpeg_bin, path, seconds=3):
    _run(ffmpeg_bin, "-y", "-f", "lavfi", "-i", f"testsrc=size=320x240:rate=25:duration={seconds}",
         "-f", "lavfi", "-i", f"sine=frequency=660:duration={seconds}", "-c:v", "libx264",
         "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path))
    return path


def test_random_moment_counts_and_title_history(client, ffmpeg_bin, sample_video):
    pid = _upload_video(client, sample_video)
    res = client.post(f"/api/projects/{pid}/generate", json={
        "theme": "Funny", "clip_length": 20, "clip_count": 3, "moments_per_clip": 4, "moments_min": 2,
    })
    assert res.status_code == 202, res.text
    done = wait_for(client, f"/api/projects/{pid}", lambda p: p["status"] in ("done", "failed"))
    assert done["status"] == "done", done
    for clip in done["clips"]:
        assert 2 <= clip["moments"] <= 4
        assert clip["title"] and len(clip["copies"]) == 1 and clip["active_copy"] == 0

    clip_id = done["clips"][0]["id"]
    first_title = done["clips"][0]["title"]
    again = client.post(f"/api/clips/{clip_id}/copy/regenerate")
    assert again.status_code == 200, again.text
    body = again.json()
    assert len(body["copies"]) == 2 and body["copies"][0]["title"] == first_title
    assert body["active_copy"] == 1
    kept = client.post(f"/api/clips/{clip_id}/copy/select", json={"index": 0})
    assert kept.status_code == 200 and kept.json()["title"] == first_title
    assert client.post(f"/api/clips/{clip_id}/copy/select", json={"index": 9}).status_code == 400


def test_guards_refuse_zero_and_impossible_moment_settings(client, sample_video):
    pid = _upload_video(client, sample_video)
    base = {"theme": "Funny", "clip_length": 15, "clip_count": 2}
    assert client.post(f"/api/projects/{pid}/generate", json={**base, "moments_per_clip": 0}).status_code == 422
    assert client.post(f"/api/projects/{pid}/generate", json={**base, "clip_count": 0}).status_code == 422
    fewest_over = client.post(f"/api/projects/{pid}/generate", json={**base, "moments_per_clip": 2, "moments_min": 3})
    assert fewest_over.status_code == 400 and "fewest" in fewest_over.json()["detail"]
    one_mix = client.post(f"/api/projects/{pid}/generate", json={**base, "moments_per_clip": 3, "moments_min": 1})
    assert one_mix.status_code == 400 and "at least 2" in one_mix.json()["detail"]
    no_intro = client.post(f"/api/projects/{pid}/generate", json={**base, "intro": True})
    assert no_intro.status_code == 400 and "intro" in no_intro.json()["detail"]


def test_intro_is_joined_before_each_clip(client, ffmpeg_bin, sample_video, tmp_path):
    intro = _short_video(ffmpeg_bin, tmp_path / "intro.mp4", seconds=3)
    with open(intro, "rb") as fh:
        res = client.post("/api/branding/intro", files={"file": ("intro.mp4", fh, "video/mp4")})
    assert res.status_code == 201, res.text
    assert res.json()["branding"]["intro"]["duration"] == 3.0
    assert client.post("/api/branding/credits", files={"file": ("x.mp4", b"x", "video/mp4")}).status_code == 404

    pid = _upload_video(client, sample_video)
    res = client.post(f"/api/projects/{pid}/generate", json={
        "theme": "Funny", "clip_length": 8, "clip_count": 1, "intro": True,
    })
    assert res.status_code == 202, res.text
    done = wait_for(client, f"/api/projects/{pid}", lambda p: p["status"] in ("done", "failed"))
    clip = done["clips"][0]
    assert clip["status"] == "done", clip
    assert clip["settings"]["bumpers"]["intro"] is True
    video = client.get(clip["download_url"])
    assert video.status_code == 200 and len(video.content) > 1000
    removed = client.delete("/api/branding/intro")
    assert removed.status_code == 200 and removed.json()["branding"]["intro"] is None
