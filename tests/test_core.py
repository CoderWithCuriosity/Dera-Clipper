"""Unit tests: media engine, captions, planner, LLM fallback, security."""
import json
import subprocess
from pathlib import Path

import pytest

from clipper import media, planner
from clipper.llm import AllProvidersFailed, InvalidOutput, LLMError, LLMProvider, LLMResult, extract_json, run_with_fallback
from clipper.security import SecurityError, check_upload_header, decrypt_secret, encrypt_secret, validate_link
from clipper.config import Settings


# -- media ---------------------------------------------------------------------

def test_probe_reports_duration_and_audio(ffmpeg_bin, sample_video):
    info = media.Ffmpeg(ffmpeg_bin).probe(sample_video)
    assert abs(info["duration"] - 40.0) < 0.5
    assert info["has_audio"] is True


def test_frame_sizes_for_each_orientation():
    assert media.frame_size("landscape", "1080p") == (1920, 1080)
    assert media.frame_size("vertical", "1080p") == (1080, 1920)
    assert media.frame_size("square", "720p") == (720, 720)
    with pytest.raises(ValueError):
        media.frame_size("diagonal", "1080p")


def _stream_size(ffmpeg_bin, path: Path) -> str:
    proc = subprocess.run([ffmpeg_bin, "-hide_banner", "-i", str(path)], capture_output=True, text=True)
    import re

    match = re.search(r"Video:.*?, (\d{3,4})x(\d{3,4})", proc.stderr)
    assert match, proc.stderr
    return f"{match.group(1)}x{match.group(2)}"


@pytest.mark.parametrize("orientation,expected", [
    ("landscape", "1280x720"), ("vertical", "720x1280"), ("square", "720x720"),
])
def test_render_clip_uses_requested_orientation(ffmpeg_bin, sample_video, tmp_path, orientation, expected):
    ff = media.Ffmpeg(ffmpeg_bin)
    out = ff.render_clip(sample_video, tmp_path / f"{orientation}.mp4", 5.0, 15.0,
                         work_dir=tmp_path / "w", orientation=orientation, resolution="720p")
    assert out.exists() and out.stat().st_size > 1000
    assert _stream_size(ffmpeg_bin, out) == expected


def test_render_clip_with_captions_mirror_and_sound_effects(ffmpeg_bin, sample_video, tmp_path):
    ff = media.Ffmpeg(ffmpeg_bin)
    sfx = tmp_path / "horn.wav"
    subprocess.run([ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", "sine=frequency=880:duration=1", str(sfx)], check=True)
    ass = media.build_captions_ass(
        [{"start": 0, "end": 4, "text": "hello there"}],
        [{"start": 0.0, "end": 0.5, "word": " hello"}, {"start": 0.5, "end": 1.0, "word": " there"}],
        0.0, 10.0, 1280, 720, "bold",
    )
    out = ff.render_clip(
        sample_video, tmp_path / "fx.mp4", 2.0, 12.0, work_dir=tmp_path / "w2",
        orientation="landscape", resolution="720p", mirror=True, captions=ass,
        has_audio=True, effects=[media.Effect(path=sfx, kind="sound", at=1.0, duration=1.0)],
    )
    assert out.exists()
    assert abs(ff.probe(out)["duration"] - 10.0) < 0.6


def test_render_clip_with_broll(ffmpeg_bin, sample_video, tmp_path):
    ff = media.Ffmpeg(ffmpeg_bin)
    clip = tmp_path / "broll.mp4"
    subprocess.run([ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", "color=c=red:size=320x240:duration=3", "-pix_fmt", "yuv420p", str(clip)], check=True)
    out = ff.render_clip(sample_video, tmp_path / "b.mp4", 0.0, 8.0, work_dir=tmp_path / "w3",
                         resolution="720p", has_audio=True,
                         effects=[media.Effect(path=clip, kind="broll", at=2.0, duration=3.0)])
    assert _stream_size(ffmpeg_bin, out) == "1280x720"


def test_captions_use_words_and_are_shifted_to_clip_time():
    ass = media.build_captions_ass(
        [{"start": 10, "end": 14, "text": "one two"}],
        [{"start": 10.0, "end": 10.5, "word": " one"}, {"start": 10.5, "end": 11.0, "word": " two"}],
        10.0, 20.0, 1080, 1920, "classic",
    )
    assert "Dialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,one two" in ass
    assert "PlayResX: 1080" in ass and "PlayResY: 1920" in ass


def test_captions_none_outside_clip():
    assert media.build_captions_ass([{"start": 50, "end": 55, "text": "x"}], None, 0, 10, 1280, 720) is None


# -- planner ---------------------------------------------------------------------

SEGMENTS = [{"start": float(i * 4), "end": float(i * 4 + 4), "text": f"sentence number {i} has some words here"}
            for i in range(30)]  # 120 seconds


def test_normalise_forces_length_and_snaps_to_boundaries():
    m = {"start": 3.5, "end": 80.0, "score": 0.9, "reason": "x"}
    starts = [s["start"] for s in SEGMENTS]
    ends = [s["end"] for s in SEGMENTS]
    out = planner.normalise_moment(m, 30.0, 120.0, starts, ends)
    assert out["start"] == 4.0 and abs((out["end"] - out["start"]) - 30.0) < 4.1
    assert out["end"] <= 120.0


def test_normalise_rejects_nothing_valid_and_clamps_end():
    out = planner.normalise_moment({"start": 110, "end": 119, "score": 0.5, "reason": ""}, 30, 120, [], [])
    assert out["end"] == 120.0 and out["start"] == 90.0


def test_validate_moments_drops_bad_entries_and_raises_when_all_bad():
    ok = planner.validate_moments({"moments": [{"start": 1, "end": 5, "score": 0.7, "reason": "fun"},
                                               {"start": "bad"}]}, 60)
    assert len(ok) == 1
    with pytest.raises(InvalidOutput):
        planner.validate_moments({"moments": [{"start": 9, "end": 2}]}, 60)
    with pytest.raises(InvalidOutput):
        planner.validate_moments({"moments": "nope"}, 60)


def test_plan_without_providers_uses_heuristic_and_never_overlaps():
    moments, used = planner.plan_moments(SEGMENTS, 120.0, "Funny", 20.0, 4, providers=[])
    assert used == "heuristic"
    assert len(moments) == 4
    spans = sorted((m.start, m.end) for m in moments)
    for (a, b), (c, d) in zip(spans, spans[1:]):
        assert b <= c


class ScriptedProvider(LLMProvider):
    def __init__(self, name, replies):
        super().__init__(name, "m")
        self.replies = list(replies)
        self.calls = 0

    def complete(self, system, user, max_tokens=3000):
        self.calls += 1
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return LLMResult(content=reply, tokens=10)


GOOD = json.dumps({"moments": [{"start": 8, "end": 28, "score": 0.95, "reason": "great joke"}]})


def test_fallback_retries_malformed_then_uses_next_provider():
    first = ScriptedProvider("first", ["not json at all", "still not json"])
    second = ScriptedProvider("second", [GOOD])
    validated, name = run_with_fallback([first, second], "sys", "user",
                                        validate=lambda o: planner.validate_moments(o, 120))
    assert name == "second"
    assert first.calls == 2  # one retry on malformed output
    assert validated[0]["reason"] == "great joke"


def test_fallback_moves_on_rate_limit_without_retry():
    from clipper.llm import LLMRateLimited

    first = ScriptedProvider("first", [LLMRateLimited("429")])
    second = ScriptedProvider("second", [GOOD])
    _, name = run_with_fallback([first, second], "s", "u", validate=lambda o: o)
    assert name == "second" and first.calls == 1


def test_all_providers_failing_raises():
    with pytest.raises(AllProvidersFailed):
        run_with_fallback([ScriptedProvider("a", [LLMError("down")])], "s", "u", validate=lambda o: o)


def test_planner_uses_model_moments_when_available():
    provider = ScriptedProvider("only", [GOOD])
    moments, used = planner.plan_moments(SEGMENTS, 120.0, "Funny", 20.0, 1, providers=[provider])
    assert used == "only"
    assert moments[0].reason == "great joke"


def test_extract_json_handles_fences_and_prose():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('Sure! Here it is: {"moments": []} hope that helps') == {"moments": []}
    with pytest.raises(InvalidOutput):
        extract_json("no braces here")


# -- security --------------------------------------------------------------------

def test_links_to_private_addresses_are_blocked():
    for url in ["http://127.0.0.1/video.mp4", "http://localhost/x.mp4", "http://10.0.0.5/a.mp4",
                "http://169.254.169.254/latest/meta-data", "ftp://example.com/a.mp4"]:
        with pytest.raises(SecurityError):
            validate_link(url)


def test_upload_header_check_rejects_text_disguised_as_video(tmp_path):
    fake = tmp_path / "fake.mp4"
    fake.write_bytes(b"this is not a video at all")
    with pytest.raises(SecurityError):
        check_upload_header(fake, ".mp4")
    real = tmp_path / "real.mp4"
    real.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 20)
    check_upload_header(real, ".mp4")


def test_api_key_round_trip_is_encrypted(app_env):
    s = Settings  # noqa: F841 - only the attribute names matter below
    from clipper.config import load_settings

    settings = load_settings()
    token = encrypt_secret(settings, "sk-secret-1234")
    assert "sk-secret" not in token
    assert decrypt_secret(settings, token) == "sk-secret-1234"
