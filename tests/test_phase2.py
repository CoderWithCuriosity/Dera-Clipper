"""Phase 2 tests: effect library, placements, Anthropic and cloud transcription adapters,
transcription fallback, and an end-to-end render with effects."""
import shutil
from pathlib import Path

import httpx
import pytest

from conftest import FakeEngine, wait_for
from clipper import effects, planner
from clipper.config import ROOT, load_settings
from clipper.db import dumps
from clipper.llm import InvalidOutput, LLMError, LLMRateLimited
from clipper.llm.anthropic import AnthropicProvider
from clipper.planner import Moment
from clipper.transcription import Transcript, TranscriptionError
from clipper.transcription import cloud


# -- helpers ---------------------------------------------------------------------

class FakeResponse:
    def __init__(self, status, body=None, headers=None, text=""):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.text = text or (str(body) if body is not None else "")

    def json(self):
        return self._body


@pytest.fixture()
def svc(app_env):
    from clipper.services import Services

    return Services.create(load_settings())


def _svc_with_effects(root: Path):
    """A service whose effects folder is `root`. Settings are frozen, so copy them."""
    import dataclasses

    from clipper.services import Services

    return Services.create(dataclasses.replace(load_settings(), effects_dir=root))


def _seed_theme_folder(root: Path, name: str, effects_list: list, manifest_extra=None):
    folder = root / name
    (folder / "sfx").mkdir(parents=True, exist_ok=True)
    (folder / "broll").mkdir(parents=True, exist_ok=True)
    manifest = {"display_name": name.title(), "effects": effects_list}
    manifest.update(manifest_extra or {})
    import json

    (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return folder


# -- effect library ----------------------------------------------------------------

def test_default_library_scans_with_no_warnings(svc):
    summary = effects.rescan(svc)
    assert summary["themes"] == 4
    assert summary["effects"] >= 9
    assert summary["warnings"] == []
    assert "Funny" in effects.list_themes(svc)
    funny = svc.db.all("SELECT * FROM effects WHERE theme = 'Funny'")
    assert {r["kind"] for r in funny} == {"sound", "broll"}
    assert all(r["duration"] and r["duration"] > 0 for r in funny)
    assert all("CC0" in (r["license"] or "") for r in funny)


def test_missing_and_unsafe_entries_are_reported_not_loaded(app_env):
    root = app_env / "lib"
    _seed_theme_folder(root, "trouble", [
        {"file": "sfx/ghost.wav", "kind": "sound"},
        {"file": "../../etc/passwd", "kind": "sound"},
        {"file": "sfx/x.wav", "kind": "video"},
    ])
    fresh = _svc_with_effects(root)
    summary = effects.rescan(fresh)
    text = " | ".join(summary["warnings"])
    assert "missing on disk" in text
    assert "must be a path inside" in text
    assert "unknown kind" in text
    assert summary["effects"] == 0


def test_speech_effects_are_transcribed_once_and_survive_a_rename(app_env, monkeypatch):
    root = app_env / "speech-lib"
    folder = _seed_theme_folder(root, "talk", [{"file": "sfx/quote.wav", "kind": "sound", "speech": True}])
    (folder / "sfx" / "quote.wav").write_bytes(b"RIFF" + b"\x00" * 4096)
    calls = {"n": 0}

    def fake_transcribe(_svc, path):
        calls["n"] += 1
        return {"text": "what a shot", "segments": [{"start": 0, "end": 1, "text": "what a shot"}]}

    monkeypatch.setattr(effects, "_transcribe_for_cache", fake_transcribe)
    fresh = _svc_with_effects(root)
    first = effects.rescan(fresh)
    assert first["transcribed"] == 1 and calls["n"] == 1

    again = effects.rescan(fresh)  # unchanged file: cache is kept
    assert again["transcribed"] == 0 and calls["n"] == 1

    # Rename the file and update the manifest. The content hash matches, so nothing is transcribed.
    (folder / "sfx" / "quote.wav").rename(folder / "sfx" / "renamed.wav")
    import json

    manifest = json.loads((folder / "manifest.json").read_text())
    manifest["effects"][0]["file"] = "sfx/renamed.wav"
    (folder / "manifest.json").write_text(json.dumps(manifest))
    renamed = effects.rescan(fresh)
    assert renamed["transcribed"] == 0 and calls["n"] == 1
    catalog = effects.catalog_for_planner(fresh, "Talk")
    assert catalog and catalog[0]["says"] == "what a shot"


def test_placements_from_model_are_checked(svc):
    effects.rescan(svc)
    catalog = {r["id"]: r for r in svc.db.all("SELECT * FROM effects WHERE theme = 'Funny'")}
    sound_id = next(k for k, v in catalog.items() if v["kind"] == "sound")
    moment = Moment(start=10.0, end=40.0, score=0.9, reason="x", effects=[
        {"effect_id": sound_id, "at": 15.0, "kind": "sound"},          # good: on a sentence end
        {"effect_id": "nope/unknown.wav", "at": 20.0, "kind": "sound"},  # unknown id: dropped
        {"effect_id": sound_id, "at": 15.5, "kind": "sound"},          # too close to the first: dropped
        {"effect_id": sound_id, "at": 39.9, "kind": "sound"},          # too close to the end: dropped
        {"effect_id": sound_id, "at": 30.0, "kind": "sound"},          # good, but not on a boundary: dropped
    ])
    from clipper.transcription import Transcript as T

    transcript = T(engine="t", language="en", segments=[
        {"start": 10.0, "end": 15.0, "text": "a"}, {"start": 15.0, "end": 25.0, "text": "b"},
        {"start": 25.0, "end": 35.0, "text": "c"}, {"start": 35.0, "end": 45.0, "text": "d"},
    ])
    effects.finalize_placements(svc, "Funny", [moment], transcript, language_model_answered=True, seed="p1")
    assert [e["at"] for e in moment.effects] == [5.0]  # relative to the clip start (10.0)


def test_fallback_when_no_model_respects_the_cap_and_spacing(svc):
    effects.rescan(svc)
    transcript = Transcript(engine="t", language="en", segments=[
        {"start": i * 3.0, "end": i * 3.0 + 3.0, "text": "x"} for i in range(40)
    ])
    moments = [Moment(start=0.0, end=60.0, score=0.5, reason="auto"), Moment(start=60.0, end=110.0, score=0.5, reason="auto")]
    effects.finalize_placements(svc, "Funny", moments, transcript, language_model_answered=False, seed="job-1")
    cap = svc.settings.max_effects_per_clip
    for m in moments:
        assert 1 <= len(m.effects) <= cap
        ats = [e["at"] for e in m.effects]
        assert all(b - a >= effects.MIN_GAP for a, b in zip(ats, ats[1:]))
        assert all(0 <= at <= (m.end - m.start) for at in ats)
    # Same seed, same result (reproducible).
    again = [Moment(start=0.0, end=60.0, score=0.5, reason="auto"), Moment(start=60.0, end=110.0, score=0.5, reason="auto")]
    effects.finalize_placements(svc, "Funny", again, transcript, language_model_answered=False, seed="job-1")
    assert [m.effects for m in again] == [m.effects for m in moments]


def test_no_effects_when_theme_has_none(svc):
    m = Moment(start=0, end=30, score=1, reason="x")
    effects.finalize_placements(svc, "Nothing Here", [m], Transcript("t", None, []), False, "s")
    assert m.effects == []


def test_planner_prompt_includes_effect_catalog():
    prompt = planner.build_user_prompt("Funny", 30, 3, [{"start": 0, "end": 2, "text": "hi"}],
                                       [{"id": "funny/sfx/airhorn.wav", "kind": "sound", "tags": ["loud"],
                                         "says": "wow"}])
    assert "funny/sfx/airhorn.wav" in prompt and 'says: "wow"' in prompt


# -- Anthropic adapter --------------------------------------------------------------

def test_anthropic_request_shape_and_parsing(monkeypatch):
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured.update(url=url, json=json, headers=headers)
        return FakeResponse(200, {"content": [{"type": "text", "text": '{"moments": []}'}],
                                  "usage": {"input_tokens": 120, "output_tokens": 30}},
                            headers={"anthropic-ratelimit-requests-remaining": "41"})

    monkeypatch.setattr(httpx, "post", fake_post)
    provider = AnthropicProvider("p", "https://api.anthropic.com/v1", "claude-sonnet-5-5", "sk-ant-test")
    result = provider.complete("sys", "usr", 500)
    assert captured["url"].endswith("/messages")
    assert captured["headers"]["x-api-key"] == "sk-ant-test"
    assert captured["headers"]["anthropic-version"] == "2023-06-01"
    assert captured["json"]["system"] == "sys"
    assert result.tokens == 150 and result.content == '{"moments": []}'
    assert result.rate_limit["anthropic-ratelimit-requests-remaining"] == "41"


def test_anthropic_errors_map_to_fallback_signals(monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda *a, **k: FakeResponse(429, {}))
    with pytest.raises(LLMRateLimited):
        AnthropicProvider("p", "", "m", "k").complete("s", "u")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: FakeResponse(401, {}))
    with pytest.raises(LLMError):
        AnthropicProvider("p", "", "m", "k").complete("s", "u")


# -- cloud transcription ----------------------------------------------------------------

def test_cloud_transcription_chunks_and_merges_timestamps(ffmpeg_bin, sample_video, tmp_path, monkeypatch):
    from clipper.media import Ffmpeg

    monkeypatch.setattr(cloud, "CHUNK_SECONDS", 15)  # 40 s video becomes 3 chunks
    calls = []

    def fake_post(url, data=None, files=None, headers=None, timeout=None):
        calls.append(files["file"][0])
        body = {"language": "en",
                "segments": [{"start": 1.0, "end": 2.0, "text": "chunk text"}],
                "words": [{"start": 1.0, "end": 1.5, "word": "chunk"}]}
        return FakeResponse(200, body)

    monkeypatch.setattr(httpx, "post", fake_post)
    engine = cloud.WhisperAPI("prov", "https://api.example.com/v1", "whisper-1", "key", Ffmpeg(ffmpeg_bin))
    audio = tmp_path / "a.mp3"
    Ffmpeg(ffmpeg_bin).extract_audio(sample_video, audio)
    transcript = engine.transcribe(audio)
    assert len(calls) == 3
    starts = [s["start"] for s in transcript.segments]
    assert starts == [1.0, 16.0, 31.0]
    assert transcript.words[1]["start"] == 16.0 and transcript.language == "en"


def test_cloud_rejected_key_raises_transcription_error(monkeypatch, tmp_path, sample_video, ffmpeg_bin):
    from clipper.media import Ffmpeg

    ff = Ffmpeg(ffmpeg_bin)
    audio = ff.extract_audio(sample_video, tmp_path / "b.mp3")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: FakeResponse(401, {}))
    engine = cloud.WhisperAPI("prov", "https://api.example.com/v1", "whisper-1", "bad", ff)
    with pytest.raises(TranscriptionError, match="rejected"):
        engine.transcribe(audio)


def test_transcription_chain_puts_cloud_first_and_local_last(svc):
    from clipper.db import now_iso
    from clipper.security import encrypt_secret

    svc.db.run(
        "INSERT INTO providers (id, user_id, kind, preset, name, base_url, model, api_key_enc, api_key_hint, "
        "priority, enabled, created_at) VALUES ('t1', 'local', 'transcription', 'groq_whisper', 'Groq', NULL, NULL, "
        "?, 'abcd', 1, 1, ?)",
        (encrypt_secret(svc.settings, "gsk_abcd"), now_iso()),
    )
    chain = svc.transcription_chain("local", "cloud")
    assert [name for name, _ in chain] == ["t1", "local"]
    local_only = svc.transcription_chain("local", "local")
    assert [name for name, _ in local_only] == ["local"]


# -- API --------------------------------------------------------------------------------

def test_effects_endpoints(client):
    listing = client.get("/api/effects").json()
    names = [t["name"] for t in listing["themes"]]
    assert "Funny" in names and "Gaming" in names
    rescan = client.post("/api/effects/rescan").json()
    assert rescan["themes"] == 4 and rescan["warnings"] == []


def test_provider_presets_include_anthropic_and_transcription(client):
    presets = client.get("/api/providers/presets").json()
    assert "anthropic" in presets["presets"]
    assert "groq_whisper" in presets["transcription_presets"]


def test_transcription_provider_needs_key(client):
    res = client.post("/api/providers", json={"kind": "transcription", "preset": "openai_whisper"})
    assert res.status_code == 400
    res = client.post("/api/providers", json={"kind": "transcription", "preset": "openai_whisper",
                                              "api_key": "sk-abcd1234"})
    assert res.status_code == 201 and res.json()["kind"] == "transcription"


def test_transcription_mode_setting(client):
    assert client.put("/api/settings", json={"values": {"transcription_mode": "maybe"}}).status_code == 400
    ok = client.put("/api/settings", json={"values": {"transcription_mode": "cloud"}})
    assert ok.json()["values"]["transcription_mode"] == "cloud"


def test_generate_with_effects_renders_clips_with_effects(client, sample_video):
    with open(sample_video, "rb") as fh:
        pid = client.post("/api/projects", files={"file": ("sample.mp4", fh, "video/mp4")}).json()["id"]
    wait_for(client, f"/api/projects/{pid}", lambda p: p["status"] == "awaiting_settings")
    client.post(f"/api/projects/{pid}/generate", json={
        "theme": "Funny", "clip_length": 12, "clip_count": 2, "captions_style": "boxed",
        "orientation": "landscape", "resolution": "720p",
    })
    done = wait_for(client, f"/api/projects/{pid}", lambda p: p["status"] in ("done", "failed"))
    assert done["status"] == "done", done
    clips = done["clips"]
    assert all(c["status"] == "done" for c in clips), clips
    placed = [e for c in clips for e in c["effects"]]
    assert placed, "expected at least one effect placement from the fallback"
    assert all(e["kind"] in ("sound", "broll") for e in placed)
    assert all(0 <= e["at"] <= 12 for e in placed)
    assert clips[0]["provider_used"] == "heuristic"
