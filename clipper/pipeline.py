"""Pipeline stages. Each job type is one handler. Handlers move the project through the
visible states: acquiring -> preparing_audio -> transcribing -> awaiting_settings ->
planning -> rendering -> done (or failed)."""
from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urljoin, urlparse

import httpx

from .db import dumps, loads, new_id, now_iso
from .media import MediaError, frame_size
from .planner import plan_moments
from .security import SecurityError, check_upload_header, validate_link
from .services import Services
from .transcription import Transcript, TranscriptionError

log = logging.getLogger("clipper.pipeline")


class TransientError(RuntimeError):
    """A temporary failure (network, a provider that is down). The job runner retries it."""


# -- helpers ---------------------------------------------------------------------

def _project(svc: Services, project_id: str) -> dict:
    row = svc.db.one("SELECT * FROM projects WHERE id = ?", (project_id,))
    if row is None:
        raise RuntimeError("The project no longer exists")
    return row


def _work_dir(svc: Services, project_id: str) -> Path:
    path = svc.settings.work_dir / project_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _load_transcript(svc: Services, project_id: str) -> Optional[Transcript]:
    row = svc.db.one("SELECT * FROM transcripts WHERE project_id = ?", (project_id,))
    if row is None:
        return None
    return Transcript(
        engine=row["engine"], language=row["language"],
        segments=loads(row["segments_json"], []), words=loads(row["words_json"], None),
    )


def build_transcription_engine(svc: Services, user_id: str, engine_name: Optional[str]):
    from .transcription import build_local_engine

    name = engine_name or svc.settings.default_transcription_engine
    if name.startswith("whisper_local") or name == "local":
        return build_local_engine(svc.settings)
    raise TranscriptionError(f"Transcription engine '{name}' is not available")


def download_link(url: str, dst: Path, max_bytes: int) -> None:
    """Stream a direct media link to disk. Every redirect hop is checked against the same
    rules as the first URL, so a redirect cannot point the server at its own network."""
    current = validate_link(url)
    for _ in range(5):
        try:
            with httpx.stream("GET", current, follow_redirects=False, timeout=60,
                              headers={"User-Agent": "AIClipper/1.0"}) as resp:
                if resp.is_redirect:
                    current = validate_link(urljoin(current, resp.headers.get("location", "")))
                    continue
                if resp.status_code >= 500:
                    raise TransientError(f"The server returned HTTP {resp.status_code}")
                if resp.status_code >= 400:
                    raise MediaError(f"The link returned HTTP {resp.status_code}")
                ctype = resp.headers.get("content-type", "")
                if "text/html" in ctype:
                    raise MediaError(
                        "This link points to a web page, not a video file. Upload the file instead, "
                        "or use a direct link to the video file."
                    )
                size = 0
                with dst.open("wb") as fh:
                    for chunk in resp.iter_bytes(1 << 20):
                        size += len(chunk)
                        if size > max_bytes:
                            raise MediaError("The video is larger than the upload limit for this mode")
                        fh.write(chunk)
                return
        except httpx.HTTPError as exc:
            raise TransientError(f"Download failed: {exc.__class__.__name__}") from exc
    raise MediaError("The link redirected too many times")


# -- handlers --------------------------------------------------------------------

def handle_download(svc: Services, job: dict) -> None:
    """Acquire the source (download a link, or confirm an upload) and read its duration."""
    pid = job["project_id"]
    p = _project(svc, pid)
    ff = svc.require_ffmpeg()
    work = _work_dir(svc, pid)

    if p["source_type"] == "link":
        svc.set_project(pid, status="acquiring", status_message="Downloading the video")
        ext = Path(unquote(urlparse(p["source_location"]).path)).suffix.lower()
        if ext not in (".mp4", ".mov", ".webm", ".mkv"):
            ext = ".mp4"
        local = work / f"source{ext}"
        download_link(p["source_location"], local, svc.settings.upload_max_mb * 1024 * 1024)
        try:
            check_upload_header(local, ext)
        except SecurityError as exc:
            raise MediaError(str(exc)) from exc
        key = f"uploads/{pid}/source{ext}"
        svc.storage.put_file(key, local)
        svc.set_project(pid, source_key=key)
    else:
        svc.set_project(pid, status="acquiring", status_message="Checking the upload")
        if not p.get("source_key"):
            raise MediaError("The uploaded file is missing")

    local = svc.storage.local_path(p["source_key"])
    info = ff.probe(local)
    if info["duration"] <= 1:
        raise MediaError("The video is too short to make clips from")
    if info["duration"] > svc.settings.max_duration_sec:
        raise MediaError(
            f"The video is longer than the {svc.settings.max_duration_sec // 60}-minute limit for this mode"
        )
    if not info["has_audio"]:
        raise MediaError("This video has no audio track, so there is nothing to transcribe")

    svc.set_project(
        pid, duration=round(info["duration"], 2), file_size=local.stat().st_size,
        status="queued", status_message="Ready for transcription",
    )
    svc.queue.enqueue(job["user_id"], pid, "transcribe")


def handle_transcribe(svc: Services, job: dict) -> None:
    pid = job["project_id"]
    p = _project(svc, pid)
    if _load_transcript(svc, pid) is not None:
        svc.set_project(pid, status="awaiting_settings", status_message="Transcript ready. Choose a theme and clip settings.")
        return

    ff = svc.require_ffmpeg()
    engine = build_transcription_engine(svc, p["user_id"], p.get("transcription_engine"))
    engine.check()
    work = _work_dir(svc, pid)
    local = svc.storage.local_path(p["source_key"])

    svc.set_project(pid, status="preparing_audio", status_message="Extracting audio", progress=0.0)
    audio = ff.extract_audio(local, work / "audio.mp3")

    svc.set_project(pid, status="transcribing", status_message="Transcribing", progress=0.0)

    def progress(fraction: float) -> None:
        svc.set_project(pid, progress=round(fraction, 3))

    try:
        transcript = engine.transcribe(audio, progress=progress)
    except TranscriptionError as exc:
        if exc.retryable:
            raise TransientError(str(exc)) from exc
        raise

    svc.db.run("DELETE FROM transcripts WHERE project_id = ?", (pid,))
    svc.db.run(
        "INSERT INTO transcripts (project_id, segments_json, words_json, engine, language, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (pid, dumps(transcript.segments), dumps(transcript.words) if transcript.words else None,
         transcript.engine, transcript.language, now_iso()),
    )
    svc.set_project(
        pid, status="awaiting_settings", progress=1.0, transcription_engine=transcript.engine,
        status_message="Transcript ready. Choose a theme and clip settings.",
    )
    audio.unlink(missing_ok=True)  # the extracted audio is temporary


def handle_plan(svc: Services, job: dict) -> None:
    pid = job["project_id"]
    p = _project(svc, pid)
    transcript = _load_transcript(svc, pid)
    if transcript is None:
        raise MediaError("There is no transcript yet for this project")

    svc.set_project(pid, status="planning", status_message="Choosing the best moments", progress=0.0)
    providers = svc.llm_providers(p["user_id"])

    def usage(name: str, result) -> None:
        svc.record_usage(name, tokens=result.tokens)

    def failure(name: str, _message: str) -> None:
        svc.record_usage(name, failed=True)

    moments, used = plan_moments(
        transcript.segments, p["duration"], p["theme"], p["clip_length"], p["clip_count"],
        providers, effects_catalog=_effects_catalog(svc, p), on_usage=usage, on_failure=failure,
    )
    if not moments:
        raise MediaError("No clip moments could be found in this video")

    svc.db.run("DELETE FROM clips WHERE project_id = ?", (pid,))
    settings_snapshot = dumps({
        "captions_style": p["captions_style"], "orientation": p["orientation"],
        "resolution": p["resolution"], "mirror": bool(p["mirror"]),
    })
    now = now_iso()
    clip_ids: list[str] = []
    statements = []
    for idx, m in enumerate(moments, start=1):
        clip_id = new_id()
        clip_ids.append(clip_id)
        statements.append((
            "INSERT INTO clips (id, project_id, idx, start_sec, end_sec, score, reason, status, provider_used, "
            "settings_json, effects_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)",
            (clip_id, pid, idx, m.start, m.end, m.score, m.reason, used, settings_snapshot,
             dumps(m.effects), now, now),
        ))
    svc.db.run_many(statements)
    svc.set_project(
        pid, status="rendering", provider_used=used, progress=0.0,
        status_message=f"Rendering {len(clip_ids)} clips",
    )
    for clip_id in clip_ids:
        svc.queue.enqueue(job["user_id"], pid, "render", {"clip_id": clip_id})


def _effects_catalog(svc: Services, project: dict) -> list[dict]:
    """Effects available for the project's theme. Filled in by the effect library."""
    from .effects import catalog_for_planner

    return catalog_for_planner(svc, project["theme"]) if project.get("theme") else []


def handle_render(svc: Services, job: dict) -> None:
    clip_id = job["payload"].get("clip_id")
    clip = svc.db.one("SELECT * FROM clips WHERE id = ?", (clip_id,))
    if clip is None:
        return
    pid = clip["project_id"]
    p = _project(svc, pid)
    svc.db.run("UPDATE clips SET status = 'rendering', updated_at = ? WHERE id = ?", (now_iso(), clip_id))
    work = _work_dir(svc, pid) / f"clip_{clip['idx']:02d}"
    try:
        ff = svc.require_ffmpeg()
        opts = loads(clip["settings_json"], {})
        width, height = frame_size(opts.get("orientation", "landscape"), opts.get("resolution", "1080p"))
        transcript = _load_transcript(svc, pid)
        style = opts.get("captions_style", "classic")
        captions = None
        if style != "off" and transcript is not None:
            from .media import build_captions_ass

            captions = build_captions_ass(transcript.segments, transcript.words,
                                          clip["start_sec"], clip["end_sec"], width, height, style)
        local = svc.storage.local_path(p["source_key"])
        has_audio = ff.probe(local)["has_audio"]
        from .media import Effect

        effects = [
            Effect(path=path, kind=e["kind"], at=float(e["at"]), duration=float(e.get("duration", 0)))
            for e, path in _resolve_effects(svc, pid, loads(clip["effects_json"], []), p["theme"],
                                             clip["end_sec"] - clip["start_sec"])
        ]
        out = work / "clip.mp4"
        work.mkdir(parents=True, exist_ok=True)
        ff.render_clip(
            local, out, clip["start_sec"], clip["end_sec"], work_dir=work,
            orientation=opts.get("orientation", "landscape"),
            resolution=opts.get("resolution", "1080p"),
            mirror=bool(opts.get("mirror")), captions=captions, has_audio=has_audio,
            effects=effects,
        )
        key = f"clips/{pid}/clip_{clip['idx']:02d}.mp4"
        svc.storage.put_file(key, out)
        svc.db.run(
            "UPDATE clips SET status = 'done', output_key = ?, error = NULL, updated_at = ? WHERE id = ?",
            (key, now_iso(), clip_id),
        )
    except Exception as exc:
        log.warning("Clip %s failed: %s", clip_id, exc)
        svc.db.run(
            "UPDATE clips SET status = 'failed', error = ?, updated_at = ? WHERE id = ?",
            (str(exc)[:1000], now_iso(), clip_id),
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)
        _update_project_progress(svc, pid)


def _resolve_effects(svc: Services, project_id: str, placements: list[dict], theme: str, clip_length: float):
    """Yield (placement, local path) pairs for placements that point at a known effect."""
    from .effects import resolve_placements

    yield from resolve_placements(svc, theme, placements, clip_length)


def _update_project_progress(svc: Services, project_id: str) -> None:
    clips = svc.db.all("SELECT status FROM clips WHERE project_id = ?", (project_id,))
    total = len(clips)
    if total == 0:
        return
    finished = sum(1 for c in clips if c["status"] in ("done", "failed"))
    done = sum(1 for c in clips if c["status"] == "done")
    if finished < total:
        svc.set_project(project_id, progress=round(finished / total, 3),
                        status_message=f"Rendered {finished} of {total} clips")
        return
    message = f"{done} of {total} clips are ready"
    if done < total:
        message += f". {total - done} failed and can be regenerated"
    svc.set_project(project_id, status="done", progress=1.0, status_message=message)


def handlers(svc: Services) -> dict:
    return {
        "download": lambda job: handle_download(svc, job),
        "transcribe": lambda job: handle_transcribe(svc, job),
        "plan": lambda job: handle_plan(svc, job),
        "render": lambda job: handle_render(svc, job),
    }
