"""HTTP API and the web interface. Every query is scoped to the current user, so the
multi-user mode only has to change how the user is identified."""
from __future__ import annotations

import logging
import shutil
import threading
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import effects
from .config import ROOT, Settings, load_settings
from .db import dumps, loads, new_id, now_iso, today
import httpx

from .llm import PRESETS
from .media import ORIENTATIONS, RESOLUTIONS
from .pipeline import handlers as pipeline_handlers
from .transcription.cloud import TRANSCRIPTION_PRESETS
from .security import (
    SecurityError, check_extension, check_upload_header, decrypt_secret, encrypt_secret, safe_filename,
    validate_link,
)
from .services import Services, build_llm_provider
from .storage import LocalStorage

log = logging.getLogger("clipper.api")

CAPTION_STYLES = ["classic", "bold", "boxed", "off"]
ACTIVE_STATES = ("queued", "acquiring", "preparing_audio", "transcribing", "planning", "rendering")
USER_SETTING_DEFAULTS = {
    "default_theme": "Funny",
    "default_clip_length": 45,
    "default_clip_count": 5,
    "default_mirror": False,
    "default_captions_style": "classic",
    "default_orientation": "landscape",
    "default_resolution": "1080p",
}


# -- request bodies --------------------------------------------------------------

class GenerateBody(BaseModel):
    theme: str
    clip_length: float = Field(gt=0)
    clip_count: int = Field(ge=1, le=20)
    mirror: bool = False
    captions_style: str = "classic"
    orientation: str = "landscape"
    resolution: str = "1080p"


class ClipSettingsBody(BaseModel):
    captions_style: Optional[str] = None
    orientation: Optional[str] = None
    resolution: Optional[str] = None
    mirror: Optional[bool] = None


class ProviderIn(BaseModel):
    kind: str = "llm"
    preset: str
    name: Optional[str] = None
    base_url: Optional[str] = None
    model: Optional[str] = None
    api_key: Optional[str] = None
    priority: int = 100
    enabled: bool = True


class ProviderPatch(BaseModel):
    name: Optional[str] = None
    base_url: Optional[str] = None
    model: Optional[str] = None
    api_key: Optional[str] = None
    priority: Optional[int] = None
    enabled: Optional[bool] = None


class SettingsPatch(BaseModel):
    values: dict


# -- app factory -----------------------------------------------------------------

def create_app(settings: Optional[Settings] = None, start_worker: Optional[bool] = None) -> FastAPI:
    settings = settings or load_settings()
    svc = Services.create(settings)
    _ensure_local_user(svc)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            summary = effects.rescan(svc)  # keep the library in step with the effects folder
            log.info("Effect library: %d effects in %d themes", summary["effects"], summary["themes"])
            for warning in summary["warnings"]:
                log.warning("Effect library: %s", warning)
        except Exception as exc:  # the app still starts without effects
            log.warning("Effect library could not be scanned: %s", exc)
        run = settings.run_worker if start_worker is None else start_worker
        if run:
            svc.queue.start(pipeline_handlers(svc), settings.worker_concurrency)
            log.info("Worker started with concurrency %d", settings.worker_concurrency)
        yield
        svc.queue.stop()

    app = FastAPI(title="AI Clipper", version="1.0.0", lifespan=lifespan)
    app.state.svc = svc

    def get_svc() -> Services:
        return svc

    def current_user(request: Request) -> dict:
        # Solo mode: one local profile. Multi-user mode replaces this function.
        return {"id": "local", "role": "admin", "email": "local@localhost"}

    user_dep = Depends(current_user)
    svc_dep = Depends(get_svc)

    # -- health and preflight ----------------------------------------------------
    @app.get("/api/health")
    def health() -> dict:
        return {"ok": True, "mode": settings.mode}

    @app.get("/api/preflight")
    def preflight() -> dict:
        from .preflight import run_checks

        return {"mode": settings.mode, "checks": run_checks(svc)}

    # -- themes and effects ------------------------------------------------------
    @app.get("/api/themes")
    def themes(s: Services = svc_dep) -> dict:
        return {"themes": effects.list_themes(s)}

    # -- settings ----------------------------------------------------------------
    @app.get("/api/settings")
    def get_settings(user: dict = user_dep, s: Services = svc_dep) -> dict:
        rows = s.db.all("SELECT key, value FROM settings WHERE user_id = ?", (user["id"],))
        values = dict(USER_SETTING_DEFAULTS, transcription_mode=settings.default_transcription_engine)
        values.update({r["key"]: loads(r["value"]) for r in rows})
        return {"values": values, "mode": settings.mode, "storage": settings.storage,
                "whisper_model": settings.whisper_model}

    @app.put("/api/settings")
    def put_settings(body: SettingsPatch, user: dict = user_dep, s: Services = svc_dep) -> dict:
        allowed = set(USER_SETTING_DEFAULTS) | {"transcription_mode"}
        for key, value in body.values.items():
            if key not in allowed:
                raise HTTPException(400, f"Unknown setting {key}")
            _validate_setting(key, value, s)
            s.db.run(
                "INSERT INTO settings (user_id, key, value) VALUES (?, ?, ?) "
                "ON CONFLICT (user_id, key) DO UPDATE SET value = excluded.value",
                (user["id"], key, dumps(value)),
            )
        return get_settings(user, s)

    # -- transcription models ----------------------------------------------------
    @app.get("/api/models")
    def models(s: Services = svc_dep) -> dict:
        from .transcription.local import MODEL_SIZES, model_status

        return {"models": [model_status(settings.models_dir, n) for n in MODEL_SIZES],
                "active": settings.whisper_model}

    @app.post("/api/models/{name}/download", status_code=202)
    def download_model(name: str, s: Services = svc_dep) -> dict:
        from .transcription.local import MODEL_SIZES, LocalWhisper

        if name not in MODEL_SIZES:
            raise HTTPException(400, "Unknown model")

        def run() -> None:
            try:
                LocalWhisper(name, settings.whisper_device, settings.models_dir).download()
            except Exception as exc:  # reported in the log; the UI polls /api/models
                log.error("Model download failed: %s", exc)

        threading.Thread(target=run, daemon=True).start()
        return {"started": True, "name": name}

    # -- providers ---------------------------------------------------------------
    @app.get("/api/providers/presets")
    def presets() -> dict:
        return {"presets": PRESETS, "transcription_presets": TRANSCRIPTION_PRESETS}

    # -- effects -------------------------------------------------------------------
    @app.get("/api/effects")
    def list_effects(s: Services = svc_dep) -> dict:
        themes = []
        for name in effects.list_themes(s):
            rows = s.db.all("SELECT id, kind, file_name, duration, tags_json, license FROM effects WHERE theme = ? ORDER BY id", (name,))
            themes.append({"name": name, "count": len(rows), "effects": [
                {"id": r["id"], "kind": r["kind"], "file": r["file_name"], "duration": r["duration"],
                 "tags": loads(r["tags_json"], []), "license": r["license"]} for r in rows]})
        return {"themes": themes, "max_effects_per_clip": settings.max_effects_per_clip}

    @app.post("/api/effects/rescan")
    def rescan_effects(s: Services = svc_dep) -> dict:
        return effects.rescan(s)

    @app.get("/api/providers")
    def list_providers(user: dict = user_dep, s: Services = svc_dep) -> dict:
        rows = s.db.all(
            "SELECT * FROM providers WHERE user_id = ? ORDER BY kind, priority, created_at", (user["id"],)
        )
        today_day = today()
        out = []
        for r in rows:
            usage = s.db.one(
                "SELECT requests, tokens, failures FROM provider_usage WHERE provider_id = ? AND day = ?",
                (r["id"], today_day),
            ) or {"requests": 0, "tokens": 0, "failures": 0}
            out.append(_provider_view(r, usage))
        return {"providers": out}

    @app.post("/api/providers", status_code=201)
    def create_provider(body: ProviderIn, user: dict = user_dep, s: Services = svc_dep) -> dict:
        if body.kind == "llm":
            preset = PRESETS.get(body.preset)
        elif body.kind == "transcription":
            preset = TRANSCRIPTION_PRESETS.get(body.preset)
        else:
            raise HTTPException(400, "kind must be 'llm' or 'transcription'")
        if preset is None:
            raise HTTPException(400, "Unknown provider preset")
        if preset.get("key_required") and not body.api_key:
            raise HTTPException(400, "This provider needs an API key")
        base_url = (body.base_url or preset.get("base_url") or "").strip()
        if body.preset == "custom" and not base_url:
            raise HTTPException(400, "Enter the base URL for the custom endpoint")
        model = (body.model or preset.get("model") or "").strip()
        if not model:
            raise HTTPException(400, "Enter a model name")
        provider_id = new_id()
        s.db.run(
            "INSERT INTO providers (id, user_id, kind, preset, name, base_url, model, api_key_enc, api_key_hint, "
            "priority, enabled, limits_note, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (provider_id, user["id"], body.kind, body.preset, (body.name or preset["label"]).strip(),
             base_url or None, model,
             encrypt_secret(settings, body.api_key) if body.api_key else None,
             body.api_key[-4:] if body.api_key else None,
             body.priority, 1 if body.enabled else 0, None, now_iso()),
        )
        return _provider_view(s.db.one("SELECT * FROM providers WHERE id = ?", (provider_id,)), None)

    @app.patch("/api/providers/{provider_id}")
    def patch_provider(provider_id: str, body: ProviderPatch, user: dict = user_dep, s: Services = svc_dep) -> dict:
        row = _owned(s, "providers", provider_id, user)
        changes: dict = {}
        if body.name is not None:
            changes["name"] = body.name.strip()
        if body.base_url is not None:
            changes["base_url"] = body.base_url.strip() or None
        if body.model is not None:
            changes["model"] = body.model.strip()
        if body.api_key:  # blank means "keep the saved key"
            changes["api_key_enc"] = encrypt_secret(settings, body.api_key)
            changes["api_key_hint"] = body.api_key[-4:]
        if body.priority is not None:
            changes["priority"] = body.priority
        if body.enabled is not None:
            changes["enabled"] = 1 if body.enabled else 0
        if changes:
            cols = ", ".join(f"{k} = ?" for k in changes)
            s.db.run(f"UPDATE providers SET {cols} WHERE id = ?", (*changes.values(), provider_id))
        return _provider_view(s.db.one("SELECT * FROM providers WHERE id = ?", (provider_id,)), None)

    @app.delete("/api/providers/{provider_id}")
    def delete_provider(provider_id: str, user: dict = user_dep, s: Services = svc_dep) -> dict:
        _owned(s, "providers", provider_id, user)
        s.db.run("DELETE FROM providers WHERE id = ?", (provider_id,))
        return {"deleted": True}

    @app.post("/api/providers/{provider_id}/test")
    def test_provider(provider_id: str, user: dict = user_dep, s: Services = svc_dep) -> dict:
        row = _owned(s, "providers", provider_id, user)
        try:
            if row["kind"] == "transcription":
                message = check_transcription_provider(settings, row)
            else:
                message = build_llm_provider(settings, row).test()
            ok, error = True, None
        except Exception as exc:
            ok, error, message = False, str(exc), None
        s.db.run(
            "UPDATE providers SET last_test_ok = ?, last_test_error = ?, last_tested_at = ? WHERE id = ?",
            (1 if ok else 0, error, now_iso(), provider_id),
        )
        return {"ok": ok, "message": message, "error": error}

    # -- projects ----------------------------------------------------------------
    @app.post("/api/projects", status_code=201)
    def create_project(
        file: Optional[UploadFile] = File(None),
        url: Optional[str] = Form(None),
        title: Optional[str] = Form(None),
        user: dict = user_dep,
        s: Services = svc_dep,
    ) -> dict:
        today_count = s.db.one(
            "SELECT COUNT(*) AS n FROM projects WHERE user_id = ? AND created_at >= ?",
            (user["id"], today()),
        )["n"]
        if today_count >= settings.max_projects_per_user_per_day:
            raise HTTPException(429, "Daily project limit reached")
        if bool(file) == bool(url and url.strip()):
            raise HTTPException(400, "Send either a file or a link, not both and not neither")

        project_id = new_id()
        now = now_iso()
        if file is not None:
            try:
                ext = check_extension(file.filename or "")
            except SecurityError as exc:
                raise HTTPException(400, str(exc))
            work = s.settings.work_dir / project_id
            work.mkdir(parents=True, exist_ok=True)
            staged = work / f"upload{ext}"
            limit = settings.upload_max_mb * 1024 * 1024
            size = 0
            with staged.open("wb") as fh:
                while True:
                    chunk = file.file.read(1 << 20)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > limit:
                        shutil.rmtree(work, ignore_errors=True)
                        raise HTTPException(413, f"The file is larger than {settings.upload_max_mb} MB")
                    fh.write(chunk)
            try:
                check_upload_header(staged, ext)
            except SecurityError as exc:
                shutil.rmtree(work, ignore_errors=True)
                raise HTTPException(400, str(exc))
            source_key = f"uploads/{project_id}/source{ext}"
            s.storage.put_file(source_key, staged)
            shutil.rmtree(work, ignore_errors=True)
            source_type, location = "upload", safe_filename(file.filename or "video")
            file_size = size
        else:
            try:
                validate_link(url or "")
            except SecurityError as exc:
                raise HTTPException(400, str(exc))
            source_type, location, source_key, file_size = "link", url.strip(), None, None

        display = (title or "").strip() or (Path(location).stem if source_type == "upload" else location[:80])
        mode_row = s.db.one("SELECT value FROM settings WHERE user_id = ? AND key = 'transcription_mode'", (user["id"],))
        transcription_mode = loads(mode_row["value"]) if mode_row else settings.default_transcription_engine
        s.db.run(
            "INSERT INTO projects (id, user_id, title, source_type, source_location, source_key, file_size, mode, "
            "status, progress, status_message, transcription_mode, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', 0, 'Waiting to start', ?, ?, ?)",
            (project_id, user["id"], display, source_type, location, source_key, file_size, settings.mode,
             transcription_mode, now, now),
        )
        s.queue.enqueue(user["id"], project_id, "download")
        return _project_view(s, s.db.one("SELECT * FROM projects WHERE id = ?", (project_id,)))

    @app.get("/api/projects")
    def list_projects(user: dict = user_dep, s: Services = svc_dep) -> dict:
        rows = s.db.all("SELECT * FROM projects WHERE user_id = ? ORDER BY created_at DESC", (user["id"],))
        return {"projects": [_project_view(s, r, with_clips=False) for r in rows]}

    @app.get("/api/projects/{project_id}")
    def get_project(project_id: str, user: dict = user_dep, s: Services = svc_dep) -> dict:
        return _project_view(s, _owned(s, "projects", project_id, user))

    @app.delete("/api/projects/{project_id}")
    def delete_project(project_id: str, user: dict = user_dep, s: Services = svc_dep) -> dict:
        row = _owned(s, "projects", project_id, user)
        if row["status"] in ACTIVE_STATES:
            raise HTTPException(409, "Wait until the project finishes before deleting it")
        s.db.run_many([
            ("DELETE FROM clips WHERE project_id = ?", (project_id,)),
            ("DELETE FROM transcripts WHERE project_id = ?", (project_id,)),
            ("DELETE FROM jobs WHERE project_id = ?", (project_id,)),
            ("DELETE FROM projects WHERE id = ?", (project_id,)),
        ])
        s.storage.delete_prefix(f"uploads/{project_id}")
        s.storage.delete_prefix(f"clips/{project_id}")
        shutil.rmtree(settings.work_dir / project_id, ignore_errors=True)
        return {"deleted": True}

    @app.get("/api/projects/{project_id}/transcript")
    def transcript(project_id: str, user: dict = user_dep, s: Services = svc_dep) -> dict:
        _owned(s, "projects", project_id, user)
        row = s.db.one("SELECT * FROM transcripts WHERE project_id = ?", (project_id,))
        if row is None:
            raise HTTPException(404, "No transcript yet")
        return {"engine": row["engine"], "language": row["language"],
                "segments": loads(row["segments_json"], []), "words": loads(row["words_json"], None)}

    @app.post("/api/projects/{project_id}/generate", status_code=202)
    def generate(project_id: str, body: GenerateBody, user: dict = user_dep, s: Services = svc_dep) -> dict:
        row = _owned(s, "projects", project_id, user)
        if row["status"] in ACTIVE_STATES:
            raise HTTPException(409, "This project is still working. Try again when it finishes.")
        if row["duration"] is None or s.db.one("SELECT 1 AS x FROM transcripts WHERE project_id = ?", (project_id,)) is None:
            raise HTTPException(409, "The transcript is not ready yet")
        if body.clip_length > row["duration"]:
            raise HTTPException(400, f"Clip length cannot exceed the video length ({row['duration']:.0f} s)")
        if body.theme not in effects.list_themes(s):
            raise HTTPException(400, "Unknown theme")
        _check_clip_options(body.captions_style, body.orientation, body.resolution)
        s.set_project(
            project_id, theme=body.theme, clip_length=body.clip_length, clip_count=body.clip_count,
            mirror=1 if body.mirror else 0, captions_style=body.captions_style,
            orientation=body.orientation, resolution=body.resolution, error=None,
            status="planning", status_message="Queued for planning", progress=0.0,
        )
        s.queue.enqueue(user["id"], project_id, "plan")
        return _project_view(s, s.db.one("SELECT * FROM projects WHERE id = ?", (project_id,)))

    @app.post("/api/projects/{project_id}/retry", status_code=202)
    def retry(project_id: str, user: dict = user_dep, s: Services = svc_dep) -> dict:
        row = _owned(s, "projects", project_id, user)
        if row["status"] != "failed":
            raise HTTPException(409, "Only failed projects can be retried")
        last = s.db.one(
            "SELECT type, payload_json FROM jobs WHERE project_id = ? AND status = 'failed' "
            "ORDER BY updated_at DESC LIMIT 1", (project_id,),
        )
        job_type = last["type"] if last else "download"
        if job_type == "render":
            job_type = "plan"
        s.set_project(project_id, status="queued", error=None, status_message="Retrying")
        s.queue.enqueue(user["id"], project_id, job_type)
        return _project_view(s, s.db.one("SELECT * FROM projects WHERE id = ?", (project_id,)))

    # -- clips -------------------------------------------------------------------
    @app.get("/api/projects/{project_id}/clips")
    def clips(project_id: str, user: dict = user_dep, s: Services = svc_dep) -> dict:
        _owned(s, "projects", project_id, user)
        return {"clips": _clip_rows(s, project_id)}

    @app.get("/api/clips/{clip_id}/video")
    def clip_video(clip_id: str, user: dict = user_dep, s: Services = svc_dep):
        clip = _clip_owned(s, clip_id, user)
        return _serve_clip(s, clip, attachment=False)

    @app.get("/api/clips/{clip_id}/download")
    def clip_download(clip_id: str, user: dict = user_dep, s: Services = svc_dep):
        clip = _clip_owned(s, clip_id, user)
        return _serve_clip(s, clip, attachment=True)

    @app.post("/api/clips/{clip_id}/regenerate", status_code=202)
    def regenerate(clip_id: str, body: ClipSettingsBody, user: dict = user_dep, s: Services = svc_dep) -> dict:
        clip = _clip_owned(s, clip_id, user)
        project = _owned(s, "projects", clip["project_id"], user)
        if project["status"] in ACTIVE_STATES:
            raise HTTPException(409, "The project is still working")
        opts = loads(clip["settings_json"], {}) or {}
        if body.captions_style is not None:
            if body.captions_style not in CAPTION_STYLES:
                raise HTTPException(400, "Unknown caption style")
            opts["captions_style"] = body.captions_style
        if body.orientation is not None:
            opts["orientation"] = body.orientation
        if body.resolution is not None:
            opts["resolution"] = body.resolution
        if body.mirror is not None:
            opts["mirror"] = body.mirror
        _check_clip_options(opts.get("captions_style", "classic"), opts.get("orientation", "landscape"),
                            opts.get("resolution", "1080p"))
        s.db.run(
            "UPDATE clips SET status = 'pending', settings_json = ?, error = NULL, output_key = NULL, updated_at = ? "
            "WHERE id = ?",
            (dumps(opts), now_iso(), clip_id),
        )
        s.set_project(project["id"], status="rendering", status_message="Regenerating a clip")
        s.queue.enqueue(user["id"], project["id"], "render", {"clip_id": clip_id})
        return {"queued": True, "clip_id": clip_id}

    @app.get("/api/projects/{project_id}/zip")
    def project_zip(project_id: str, user: dict = user_dep, s: Services = svc_dep):
        project = _owned(s, "projects", project_id, user)
        done = s.db.all(
            "SELECT * FROM clips WHERE project_id = ? AND status = 'done' ORDER BY idx", (project_id,)
        )
        if not done:
            raise HTTPException(404, "No finished clips yet")
        work = settings.work_dir / project_id
        work.mkdir(parents=True, exist_ok=True)
        archive = work / "clips.zip"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as zf:
            for clip in done:
                path = s.storage.local_path(clip["output_key"])
                zf.write(path, arcname=f"clip_{clip['idx']:02d}_score_{(clip['score'] or 0):.2f}.mp4")
        return FileResponse(archive, media_type="application/zip",
                            filename=f"{safe_filename(project['title'])}-clips.zip")

    # -- interface ---------------------------------------------------------------
    web_dir = ROOT / "web"
    if web_dir.exists():
        app.mount("/", StaticFiles(directory=str(web_dir), html=True), name="web")

    return app


# -- helpers ---------------------------------------------------------------------

def _ensure_local_user(svc: Services) -> None:
    if svc.db.one("SELECT id FROM users WHERE id = 'local'") is None:
        svc.db.run(
            "INSERT INTO users (id, email, password_hash, role, disabled, created_at) "
            "VALUES ('local', 'local@localhost', NULL, 'admin', 0, ?)",
            (now_iso(),),
        )


def _validate_setting(key: str, value, svc: Services) -> None:
    if key == "transcription_mode" and value not in ("local", "cloud"):
        raise HTTPException(400, "Transcription mode must be 'local' or 'cloud'")
    if key == "default_theme" and value not in effects.list_themes(svc):
        raise HTTPException(400, "Unknown theme")
    if key == "default_clip_length" and (not isinstance(value, (int, float)) or value <= 0):
        raise HTTPException(400, "Clip length must be greater than zero")
    if key == "default_clip_count" and (not isinstance(value, int) or not 1 <= value <= 20):
        raise HTTPException(400, "Clip count must be between 1 and 20")
    if key == "default_captions_style" and value not in CAPTION_STYLES:
        raise HTTPException(400, "Unknown caption style")
    if key == "default_orientation" and value not in ORIENTATIONS:
        raise HTTPException(400, "Unknown orientation")
    if key == "default_resolution" and value not in RESOLUTIONS:
        raise HTTPException(400, "Unknown resolution")


def _check_clip_options(captions: str, orientation: str, resolution: str) -> None:
    if captions not in CAPTION_STYLES:
        raise HTTPException(400, "Unknown caption style")
    if orientation not in ORIENTATIONS:
        raise HTTPException(400, "Unknown orientation")
    if resolution not in RESOLUTIONS:
        raise HTTPException(400, "Unknown resolution")


def _owned(svc: Services, table: str, row_id: str, user: dict) -> dict:
    row = svc.db.one(f"SELECT * FROM {table} WHERE id = ?", (row_id,))
    if row is None or row.get("user_id") != user["id"]:
        raise HTTPException(404, "Not found")
    return row


def _clip_owned(svc: Services, clip_id: str, user: dict) -> dict:
    clip = svc.db.one("SELECT * FROM clips WHERE id = ?", (clip_id,))
    if clip is None:
        raise HTTPException(404, "Not found")
    _owned(svc, "projects", clip["project_id"], user)
    return clip


def _serve_clip(svc: Services, clip: dict, attachment: bool):
    if clip["status"] != "done" or not clip.get("output_key"):
        raise HTTPException(404, "This clip is not ready")
    url = svc.storage.download_url(clip["output_key"], f"clip_{clip['idx']:02d}.mp4")
    if url:
        return RedirectResponse(url)
    path = svc.storage.local_path(clip["output_key"])
    filename = f"clip_{clip['idx']:02d}.mp4"
    if attachment:
        return FileResponse(path, media_type="video/mp4", filename=filename)
    return FileResponse(path, media_type="video/mp4", headers={"Content-Disposition": f'inline; filename="{filename}"'})


def check_transcription_provider(settings: Settings, row: dict) -> str:
    """Check the key with a cheap authenticated request that does not use any audio."""
    preset = TRANSCRIPTION_PRESETS.get(row["preset"], {})
    base = (row.get("base_url") or preset.get("base_url") or "").rstrip("/")
    api_key = decrypt_secret(settings, row["api_key_enc"]) if row.get("api_key_enc") else ""
    try:
        resp = httpx.get(f"{base}/models", headers={"Authorization": f"Bearer {api_key}"}, timeout=20)
    except httpx.HTTPError as exc:
        raise RuntimeError(f"Network error: {exc.__class__.__name__}") from exc
    if resp.status_code in (401, 403):
        raise RuntimeError("The API key was rejected")
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code}")
    return "Connection OK"


def _provider_view(row: dict, usage: Optional[dict]) -> dict:
    preset = PRESETS.get(row["preset"]) or TRANSCRIPTION_PRESETS.get(row["preset"]) or {}
    rate = loads(row.get("last_rate_limit"), None)
    return {
        "id": row["id"],
        "kind": row["kind"],
        "preset": row["preset"],
        "preset_label": preset.get("label", row["preset"]),
        "name": row["name"],
        "base_url": row.get("base_url") or preset.get("base_url"),
        "model": row.get("model") or preset.get("model"),
        "priority": row["priority"],
        "enabled": bool(row["enabled"]),
        "key_hint": ("••••" + row["api_key_hint"]) if row.get("api_key_hint") else "",
        "last_test_ok": None if row.get("last_test_ok") is None else bool(row["last_test_ok"]),
        "last_test_error": row.get("last_test_error"),
        "last_tested_at": row.get("last_tested_at"),
        "limits_note": row.get("limits_note"),
        "last_rate_limit": rate,
        "docs_url": preset.get("docs") or None,
        "usage_today": usage,
    }


def _clip_rows(svc: Services, project_id: str) -> list[dict]:
    rows = svc.db.all("SELECT * FROM clips WHERE project_id = ? ORDER BY idx", (project_id,))
    out = []
    for r in rows:
        out.append({
            "id": r["id"], "idx": r["idx"], "start": r["start_sec"], "end": r["end_sec"],
            "length": round(r["end_sec"] - r["start_sec"], 2),
            "score": r["score"], "reason": r["reason"], "status": r["status"], "error": r["error"],
            "provider_used": r["provider_used"], "settings": loads(r["settings_json"], {}),
            "effects": loads(r["effects_json"], []), "views": r["views"], "likes": r["likes"],
            "video_url": f"/api/clips/{r['id']}/video" if r["status"] == "done" else None,
            "download_url": f"/api/clips/{r['id']}/download" if r["status"] == "done" else None,
        })
    return out


def _project_view(svc: Services, row: dict, with_clips: bool = True) -> dict:
    view = {
        "id": row["id"],
        "title": row["title"],
        "source_type": row["source_type"],
        "source_location": row["source_location"],
        "duration": row["duration"],
        "file_size": row["file_size"],
        "mode": row["mode"],
        "status": row["status"],
        "progress": row["progress"],
        "status_message": row["status_message"],
        "theme": row["theme"],
        "clip_length": row["clip_length"],
        "clip_count": row["clip_count"],
        "mirror": bool(row["mirror"]),
        "captions_style": row["captions_style"],
        "orientation": row["orientation"],
        "resolution": row["resolution"],
        "transcription_engine": row["transcription_engine"],
        "provider_used": row["provider_used"],
        "error": row["error"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "has_transcript": svc.db.one("SELECT 1 AS x FROM transcripts WHERE project_id = ?", (row["id"],)) is not None,
    }
    if with_clips:
        view["clips"] = _clip_rows(svc, row["id"])
    return view
