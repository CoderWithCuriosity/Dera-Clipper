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
from .planner import HEURISTIC, clip_parts, group_highlights, plan_moments
from .security import SecurityError, check_upload_header, is_platform_link, validate_link
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


def build_transcription_chain(svc: Services, user_id: str, mode: str) -> list:
    """Ordered (name, engine) pairs. Each one is tried until one succeeds."""
    return svc.transcription_chain(user_id, mode)


def download_platform_link(url: str, work: Path, ffmpeg_location: str, max_bytes: int,
                           cookies_file: Optional[Path] = None) -> Path:
    """Download a YouTube video with yt-dlp. Only a single video is taken (never a playlist),
    capped at 1080p and at the upload size limit. Runs only when the operator has enabled it."""
    try:
        import yt_dlp
    except ImportError as exc:
        raise MediaError("yt-dlp is not installed on this server. Run: pip install yt-dlp") from exc
    template = str(work / "platform.%(ext)s")
    options = {
        # YouTube serves video and audio as separate streams, so merge them (needs FFmpeg).
        "format": "bv*[ext=mp4][height<=1080]+ba[ext=m4a]/bv*[height<=1080]+ba/b[height<=1080]/b",
        "merge_output_format": "mp4",
        "outtmpl": template,
        "noplaylist": True,
        "max_filesize": max_bytes,
        "ffmpeg_location": ffmpeg_location,
        "retries": 2,
        "socket_timeout": 60,
        "quiet": True,
        "no_progress": True,
        "no_warnings": True,
        "restrictfilenames": True,
    }
    if cookies_file and cookies_file.is_file():
        options["cookiefile"] = str(cookies_file)
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            ydl.download([url])
    except yt_dlp.utils.DownloadError as exc:
        raise MediaError("YouTube did not return this video: " + str(exc).replace("ERROR: ", "")[:240]) from exc
    found = sorted(work.glob("platform.*"))
    found = [f for f in found if f.suffix.lower() in (".mp4", ".mov", ".webm", ".mkv")]
    if not found:
        raise MediaError("The download finished but no video file was found")
    return found[0]


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
        max_bytes = svc.settings.upload_max_mb * 1024 * 1024
        if is_platform_link(p["source_location"]):
            if not svc.settings.allow_platform_links:
                raise MediaError("YouTube links are turned off on this server")
            svc.set_project(pid, status="acquiring", status_message="Downloading from YouTube")
            local = download_platform_link(p["source_location"], work, svc.require_ffmpeg().binary, max_bytes,
                                                   svc.settings.youtube_cookies_file)
            ext = local.suffix.lower()
        else:
            svc.set_project(pid, status="acquiring", status_message="Downloading the video")
            ext = Path(unquote(urlparse(p["source_location"]).path)).suffix.lower()
            if ext not in (".mp4", ".mov", ".webm", ".mkv"):
                ext = ".mp4"
            local = work / f"source{ext}"
            download_link(p["source_location"], local, max_bytes)
        if ext not in (".mp4", ".mov", ".webm", ".mkv"):
            raise MediaError(f"The downloaded file type {ext} is not supported")
        try:
            check_upload_header(local, ext)
        except SecurityError as exc:
            raise MediaError(str(exc)) from exc
        key = f"uploads/{pid}/source{ext}"
        svc.storage.put_file(key, local)
        svc.set_project(pid, source_key=key)
        p = _project(svc, pid)  # reload: the storage key was just saved
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
    chain = build_transcription_chain(svc, p["user_id"], p.get("transcription_mode") or "local")
    work = _work_dir(svc, pid)
    local = svc.storage.local_path(p["source_key"])

    svc.set_project(pid, status="preparing_audio", status_message="Extracting audio", progress=0.0)
    audio = ff.extract_audio(local, work / "audio.mp3")

    svc.set_project(pid, status="transcribing", status_message="Transcribing", progress=0.0)

    def progress(fraction: float) -> None:
        svc.set_project(pid, progress=round(fraction, 3))

    transcript: Optional[Transcript] = None
    errors: list[str] = []
    for name, engine in chain:
        try:
            engine.check()
            transcript = engine.transcribe(audio, progress=progress)
            if name != "local":
                svc.record_usage(name)
            break
        except TranscriptionError as exc:
            errors.append(f"{name}: {exc}")
            log.warning("Transcription with %s failed: %s", name, exc)
            if name != "local":
                svc.record_usage(name, failed=True)
    if transcript is None:
        audio.unlink(missing_ok=True)
        raise TranscriptionError("No transcription engine could run. " + "; ".join(errors))

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
    _apply_batch_settings(svc, pid)


def _apply_batch_settings(svc: Services, project_id: str) -> None:
    """If the project belongs to a batch that already has settings, start generating now."""
    from .services import start_generation

    project = _project(svc, project_id)
    if not project.get("batch_id"):
        return
    batch = svc.db.one("SELECT settings_json FROM batches WHERE id = ?", (project["batch_id"],))
    opts = loads(batch["settings_json"], None) if batch else None
    if not opts:
        return
    try:
        start_generation(svc, project, opts)
    except ValueError as exc:
        svc.set_project(project_id, status_message=f"Transcript ready. Batch settings were not applied: {exc}")


def handle_plan(svc: Services, job: dict) -> None:
    pid = job["project_id"]
    p = _project(svc, pid)
    transcript = _load_transcript(svc, pid)
    if transcript is None:
        raise MediaError("There is no transcript yet for this project")

    svc.set_project(pid, status="planning", status_message="Choosing the best moments", progress=0.0)
    providers = svc.llm_providers(p["user_id"])

    def usage(name: str, result) -> None:
        svc.record_usage(name, tokens=result.tokens, rate_limit=result.rate_limit)

    def failure(name: str, _message: str) -> None:
        svc.record_usage(name, failed=True)

    # Mixed highlights: each clip joins several moments, so the planner finds per_clip times as
    # many moments, each one a share of the requested clip length. Single clips use the plain path.
    per_clip = max(1, int(p.get("moments_per_clip") or 1))
    mixed = per_clip > 1
    if mixed:
        moment_length, wanted = p["clip_length"] / per_clip, p["clip_count"] * per_clip
        catalog: list[dict] = []  # sound effects and B-roll are added to single-moment clips only
    else:
        moment_length, wanted = p["clip_length"], p["clip_count"]
        catalog = _effects_catalog(svc, p)

    moments, used = plan_moments(
        transcript.segments, p["duration"], p["theme"], moment_length, wanted,
        providers, effects_catalog=catalog, on_usage=usage, on_failure=failure,
    )
    if not moments:
        raise MediaError("No clip moments could be found in this video")
    groups = group_highlights(moments, per_clip, p["clip_count"]) if mixed else [[m] for m in moments]

    from .effects import finalize_placements

    if p.get("effects_enabled", 1) and not mixed:
        finalize_placements(
            svc, p["theme"], moments, transcript,
            language_model_answered=used != HEURISTIC, seed=pid,
        )
    else:
        for m in moments:
            m.effects = []

    svc.db.run("DELETE FROM clips WHERE project_id = ?", (pid,))
    settings_snapshot = dumps({
        "captions_style": p["captions_style"], "orientation": p["orientation"],
        "resolution": p["resolution"], "mirror": bool(p["mirror"]),
        "logo": loads(p.get("logo_json"), None),
    })
    now = now_iso()
    clip_ids: list[str] = []
    statements = []
    for idx, group in enumerate(groups, start=1):
        clip_id = new_id()
        clip_ids.append(clip_id)
        first = group[0]
        reason = first.reason if len(group) == 1 else " · ".join(m.reason for m in group if m.reason)[:1000]
        score = sum(m.score for m in group) / len(group)
        segments_json = dumps([[m.start, m.end] for m in group]) if mixed else None
        statements.append((
            "INSERT INTO clips (id, project_id, idx, start_sec, end_sec, score, reason, status, provider_used, "
            "settings_json, effects_json, segments_json, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?)",
            (clip_id, pid, idx, min(m.start for m in group), max(m.end for m in group), round(score, 3),
             reason, used, settings_snapshot, dumps(first.effects), segments_json, now, now),
        ))
    svc.db.run_many(statements)
    message = f"Rendering {len(clip_ids)} clips"
    if len(clip_ids) < p["clip_count"]:
        if mixed:
            message += f" (only {len(clip_ids)} of {p['clip_count']} could be built from the moments found)"
        else:
            message += (f" (only {len(clip_ids)} of {p['clip_count']} fit without overlapping in this video)")
    svc.set_project(pid, status="rendering", provider_used=used, progress=0.0, status_message=message)
    for clip_id in clip_ids:
        svc.queue.enqueue(job["user_id"], pid, "render", {"clip_id": clip_id})


def _effects_catalog(svc: Services, project: dict) -> list[dict]:
    """Effects available for the project's theme. Filled in by the effect library."""
    if not project.get("effects_enabled", 1):
        return []
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
        orientation = opts.get("orientation", "landscape")
        resolution = opts.get("resolution", "1080p")
        width, height = frame_size(orientation, resolution)
        parts = clip_parts(clip["start_sec"], clip["end_sec"], clip.get("segments_json"))
        transcript = _load_transcript(svc, pid)
        style = opts.get("captions_style", "classic")
        captions = None
        if style != "off" and transcript is not None:
            from .media import build_mix_captions_ass

            captions = build_mix_captions_ass(transcript.segments, transcript.words, parts, width, height, style)
        local = svc.storage.local_path(p["source_key"])
        has_audio = ff.probe(local)["has_audio"]
        logo = _resolve_logo(svc, p["user_id"], opts.get("logo"))
        out = work / "clip.mp4"
        work.mkdir(parents=True, exist_ok=True)
        if len(parts) > 1:
            ff.render_mix(
                local, out, parts, work_dir=work, orientation=orientation, resolution=resolution,
                mirror=bool(opts.get("mirror")), captions=captions, has_audio=has_audio, logo=logo,
            )
        else:
            from .media import Effect

            effects = [
                Effect(path=path, kind=e["kind"], at=float(e["at"]), duration=float(e.get("duration", 0)))
                for e, path in _resolve_effects(svc, pid, loads(clip["effects_json"], []), p["theme"],
                                                 clip["end_sec"] - clip["start_sec"])
            ]
            ff.render_clip(
                local, out, clip["start_sec"], clip["end_sec"], work_dir=work,
                orientation=orientation, resolution=resolution,
                mirror=bool(opts.get("mirror")), captions=captions, has_audio=has_audio,
                effects=effects, logo=logo,
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


def _resolve_logo(svc: Services, user_id: str, saved: Optional[dict]):
    """The logo for a clip, or None when it is off or the user's logo file is gone."""
    from .branding import clean_logo, logo_key
    from .media import Logo

    logo = clean_logo(saved)
    key = logo_key(user_id)
    if not logo["enabled"] or not svc.storage.exists(key):
        return None
    return Logo(path=svc.storage.local_path(key), opacity=logo["opacity"], size=logo["size"],
                x=logo["x"], y=logo["y"])


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
