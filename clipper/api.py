"""HTTP API and the web interface. Every query is scoped to the current user, so the
multi-user mode only has to change how the user is identified."""
from __future__ import annotations

import logging
import os
import re
import shutil
import threading
import zipfile
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import effects, library
from .auth import (
    AuthError, RateLimiter, check_password_rules, hash_password, issue_access_token, issue_media_token,
    issue_refresh_token, read_access_token, read_media_token, revoke_all, rotate_refresh_token, verify_password,
)
from .config import ROOT, Settings, load_settings
from .db import dumps, loads, new_id, now_iso, today
import httpx

from .branding import LOGO_MAX_BYTES, MOMENTS_MAX, check_logo_file, logo_key
from .llm import PRESETS
from .media import ORIENTATIONS, RESOLUTIONS, MediaError
from .pipeline import handlers as pipeline_handlers
from .planner import clip_parts
from .transcription.cloud import TRANSCRIPTION_PRESETS
from .security import (
    SecurityError, check_extension, check_upload_header, decrypt_secret, encrypt_secret, is_platform_link,
    safe_filename, validate_link,
)
from .services import Services, build_llm_provider, queue_info, server_load, start_generation
from .storage import LocalStorage

log = logging.getLogger("clipper.api")

CAPTION_STYLES = ["classic", "bold", "boxed", "karaoke", "off"]
ACTIVE_STATES = ("queued", "acquiring", "preparing_audio", "transcribing", "planning", "rendering")
USER_SETTING_DEFAULTS = {
    "default_theme": "Funny",
    "default_clip_length": 45,
    "default_clip_count": 5,
    "default_mirror": False,
    "default_captions_style": "classic",
    "default_orientation": "landscape",
    "default_resolution": "1080p",
    "default_moments_per_clip": 1,
    "default_logo_on": False,
    "default_logo_opacity": 80,
    "default_logo_size": 15,
    "default_logo_x": 97,
    "default_logo_y": 3,
}


# -- request bodies --------------------------------------------------------------

class LogoBody(BaseModel):
    enabled: bool = False
    opacity: float = Field(default=80, ge=0, le=100)
    size: float = Field(default=15, ge=5, le=50)     # percent of the frame width
    x: float = Field(default=97, ge=0, le=100)       # 0 = left edge, 100 = right edge
    y: float = Field(default=3, ge=0, le=100)        # 0 = top edge, 100 = bottom edge


class GenerateBody(BaseModel):
    theme: str
    clip_length: float = Field(gt=0)
    clip_count: int = Field(ge=1, le=20)
    mirror: bool = False
    effects: bool = True
    captions_style: str = "classic"
    orientation: str = "landscape"
    resolution: str = "1080p"
    moments_per_clip: int = Field(default=1, ge=1, le=MOMENTS_MAX)  # 1 = single moments
    logo: Optional[LogoBody] = None


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
    verify: bool = True  # send a small test request before the provider is saved


class ProviderPatch(BaseModel):
    name: Optional[str] = None
    base_url: Optional[str] = None
    model: Optional[str] = None
    api_key: Optional[str] = None
    priority: Optional[int] = None
    enabled: Optional[bool] = None


class SettingsPatch(BaseModel):
    values: dict


class AuthBody(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=256)


class PasswordBody(BaseModel):
    current_password: str
    new_password: str


class MetricsBody(BaseModel):
    views: int = Field(ge=0, le=10**12)
    likes: int = Field(default=0, ge=0, le=10**12)


class BatchIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class ThemeIn(BaseModel):
    name: str = Field(min_length=2, max_length=60)
    description: str = Field(default="", max_length=300)


class AdminUserPatch(BaseModel):
    role: Optional[str] = None
    disabled: Optional[bool] = None


# -- app factory -----------------------------------------------------------------

def create_app(settings: Optional[Settings] = None, start_worker: Optional[bool] = None) -> FastAPI:
    settings = settings or load_settings()
    svc = Services.create(settings)
    if not settings.require_auth:
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
        maintenance = None
        if run:
            svc.queue.start(pipeline_handlers(svc), settings.worker_concurrency)
            log.info("Worker started with concurrency %d", settings.worker_concurrency)
            if settings.maintenance:
                from .maintenance import Maintenance

                maintenance = Maintenance(svc)
                maintenance.start()
        yield
        if maintenance:
            maintenance.stop()
        svc.queue.stop()

    app = FastAPI(title="AI Clipper", version="1.0.0", lifespan=lifespan)
    app.state.svc = svc

    def get_svc() -> Services:
        return svc

    limiter = RateLimiter()

    def current_user(request: Request) -> dict:
        if not settings.require_auth:
            # Solo mode: one local profile, no sign-in (the app listens on localhost by default).
            user = {"id": "local", "role": "admin", "email": "local@localhost"}
        else:
            header = request.headers.get("authorization", "")
            if not header.lower().startswith("bearer "):
                raise HTTPException(401, "Sign in to continue")
            payload = read_access_token(settings, header[7:].strip())
            if payload is None:
                raise HTTPException(401, "Your session has expired. Sign in again.")
            row = svc.db.one("SELECT * FROM users WHERE id = ?", (payload["sub"],))
            if row is None or row["disabled"]:
                raise HTTPException(401, "This account is not available")
            user = {"id": row["id"], "role": row["role"], "email": row["email"]}
        if not limiter.allow(f"api:{user['id']}", settings.api_requests_per_minute, 60):
            raise HTTPException(429, "Too many requests. Wait a minute and try again.")
        return user

    def require_admin(user: dict = Depends(current_user)) -> dict:
        if user["role"] != "admin":
            raise HTTPException(403, "Only administrators can do that")
        return user

    user_dep = Depends(current_user)
    admin_dep = Depends(require_admin)

    def _media_user(request: Request, ref: str) -> dict:
        """Browser requests for video and downloads: a sign-in header, or a signed link for this item."""
        header = request.headers.get("authorization", "")
        if header.lower().startswith("bearer ") or not settings.require_auth:
            return current_user(request)
        token = request.query_params.get("t", "")
        payload = read_media_token(settings, token, ref) if token else None
        if payload is None:
            raise HTTPException(401, "This link has expired. Refresh the page and try again.")
        row = svc.db.one("SELECT * FROM users WHERE id = ?", (payload["sub"],))
        if row is None or row["disabled"]:
            raise HTTPException(401, "This account is not available")
        return {"id": row["id"], "role": row["role"], "email": row["email"]}

    def clip_media_user(request: Request, clip_id: str) -> dict:
        return _media_user(request, f"clip:{clip_id}")

    def project_media_user(request: Request, project_id: str) -> dict:
        return _media_user(request, f"project:{project_id}")

    clip_media_dep = Depends(clip_media_user)
    project_media_dep = Depends(project_media_user)

    def effect_media_user(request: Request, effect_id: str) -> dict:
        return _media_user(request, f"effect:{effect_id}")

    effect_media_dep = Depends(effect_media_user)
    svc_dep = Depends(get_svc)

    # -- accounts ------------------------------------------------------------------
    @app.post("/api/auth/register", status_code=201)
    def register(body: AuthBody, request: Request, response: Response, s: Services = svc_dep) -> dict:
        if not settings.require_auth:
            raise HTTPException(400, "Sign-in is turned off in this mode")
        if not settings.allow_registration:
            raise HTTPException(403, "Registration is closed. Ask an administrator for an account.")
        email = _clean_email(body.email)
        try:
            check_password_rules(body.password)
        except AuthError as exc:
            raise HTTPException(exc.status, str(exc))
        if s.db.one("SELECT id FROM users WHERE email = ?", (email,)):
            raise HTTPException(409, "An account with this email already exists")
        first = s.db.one("SELECT COUNT(*) AS n FROM users WHERE id <> 'local'")["n"] == 0
        role = "admin" if first or (settings.admin_email and email == settings.admin_email) else "user"
        user_id = new_id()
        s.db.run(
            "INSERT INTO users (id, email, password_hash, role, disabled, created_at) VALUES (?, ?, ?, ?, 0, ?)",
            (user_id, email, hash_password(body.password), role, now_iso()),
        )
        return _start_session(s, user_id, email, role, response)

    @app.post("/api/auth/login")
    def login(body: AuthBody, request: Request, response: Response, s: Services = svc_dep) -> dict:
        if not settings.require_auth:
            raise HTTPException(400, "Sign-in is turned off in this mode")
        email = _clean_email(body.email)
        ip = request.client.host if request.client else "unknown"
        if not limiter.allow(f"login:{ip}:{email}", settings.login_attempts_per_15_min, 15 * 60):
            raise HTTPException(429, "Too many sign-in attempts. Try again in 15 minutes.")
        row = s.db.one("SELECT * FROM users WHERE email = ?", (email,))
        if row is None or not verify_password(body.password, row["password_hash"]):
            raise HTTPException(401, "Email or password is wrong")
        if row["disabled"]:
            raise HTTPException(403, "This account has been disabled")
        return _start_session(s, row["id"], email, row["role"], response)

    @app.post("/api/auth/refresh")
    def refresh(request: Request, response: Response, s: Services = svc_dep) -> dict:
        token = request.cookies.get("clipper_refresh")
        user_id = rotate_refresh_token(s.db, token) if token else None
        if user_id is None:
            response.delete_cookie("clipper_refresh", path="/api/auth")
            raise HTTPException(401, "Sign in to continue")
        row = s.db.one("SELECT * FROM users WHERE id = ?", (user_id,))
        if row is None or row["disabled"]:
            raise HTTPException(401, "Sign in to continue")
        return _start_session(s, row["id"], row["email"], row["role"], response)

    @app.post("/api/auth/logout")
    def logout(request: Request, response: Response, s: Services = svc_dep) -> dict:
        token = request.cookies.get("clipper_refresh")
        if token:
            user_id = rotate_refresh_token(s.db, token)
            if user_id:
                revoke_all(s.db, user_id)
        response.delete_cookie("clipper_refresh", path="/api/auth")
        return {"signed_out": True}

    @app.get("/api/auth/me")
    def me(user: dict = user_dep) -> dict:
        return {"user": user, "auth_required": settings.require_auth}

    @app.post("/api/auth/change-password")
    def change_password(body: PasswordBody, user: dict = user_dep, s: Services = svc_dep) -> dict:
        row = s.db.one("SELECT * FROM users WHERE id = ?", (user["id"],))
        if row is None or not verify_password(body.current_password, row["password_hash"]):
            raise HTTPException(400, "The current password is wrong")
        try:
            check_password_rules(body.new_password)
        except AuthError as exc:
            raise HTTPException(exc.status, str(exc))
        s.db.run("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(body.new_password), user["id"]))
        revoke_all(s.db, user["id"])
        return {"changed": True, "message": "Password changed. Sign in again on other devices."}

    # -- administration ------------------------------------------------------------
    @app.get("/api/admin/users")
    def admin_users(_admin: dict = admin_dep, s: Services = svc_dep) -> dict:
        rows = s.db.all("SELECT id, email, role, disabled, created_at FROM users ORDER BY created_at")
        out = []
        for r in rows:
            projects = s.db.one("SELECT COUNT(*) AS n FROM projects WHERE user_id = ?", (r["id"],))["n"]
            out.append({**r, "disabled": bool(r["disabled"]), "projects": projects})
        return {"users": out}

    @app.patch("/api/admin/users/{user_id}")
    def admin_update_user(user_id: str, body: AdminUserPatch, admin: dict = admin_dep, s: Services = svc_dep) -> dict:
        row = s.db.one("SELECT * FROM users WHERE id = ?", (user_id,))
        if row is None:
            raise HTTPException(404, "Not found")
        changes: dict = {}
        if body.role is not None:
            if body.role not in ("admin", "user"):
                raise HTTPException(400, "Role must be 'admin' or 'user'")
            if row["role"] == "admin" and body.role != "admin" and _admin_count(s) <= 1:
                raise HTTPException(400, "There must be at least one administrator")
            changes["role"] = body.role
        if body.disabled is not None:
            if body.disabled and user_id == admin["id"]:
                raise HTTPException(400, "You cannot disable your own account")
            changes["disabled"] = 1 if body.disabled else 0
            if body.disabled:
                revoke_all(s.db, user_id)
        if changes:
            cols = ", ".join(f"{k} = ?" for k in changes)
            s.db.run(f"UPDATE users SET {cols} WHERE id = ?", (*changes.values(), user_id))
        return {"updated": True}

    @app.get("/api/admin/stats")
    def admin_stats(_admin: dict = admin_dep, s: Services = svc_dep) -> dict:
        def count(sql: str, params: tuple = ()) -> int:
            return s.db.one(sql, params)["n"]

        return {
            "users": count("SELECT COUNT(*) AS n FROM users"),
            "projects": count("SELECT COUNT(*) AS n FROM projects"),
            "projects_by_status": {r["status"]: r["n"] for r in s.db.all(
                "SELECT status, COUNT(*) AS n FROM projects GROUP BY status")},
            "jobs_by_status": {r["status"]: r["n"] for r in s.db.all(
                "SELECT status, COUNT(*) AS n FROM jobs GROUP BY status")},
            "clips_done": count("SELECT COUNT(*) AS n FROM clips WHERE status = 'done'"),
            "providers": count("SELECT COUNT(*) AS n FROM providers"),
            "stored_upload_mb": round((s.db.one("SELECT COALESCE(SUM(file_size), 0) AS n FROM projects")["n"] or 0) / 1048576, 1),
        }

    @app.get("/api/admin/backups")
    def admin_backups(_admin: dict = admin_dep) -> dict:
        from .backup import list_backups

        return {"backups": list_backups(settings)}

    @app.post("/api/admin/backups", status_code=201)
    def admin_backup_now(_admin: dict = admin_dep) -> dict:
        from .backup import create_backup

        try:
            path = create_backup(settings)
        except Exception as exc:
            raise HTTPException(500, str(exc))
        return {"name": path.name}

    # -- health and preflight ----------------------------------------------------
    @app.get("/api/health")
    def health() -> dict:
        return {"ok": True, "mode": settings.mode, "platform_links": settings.allow_platform_links}

    @app.get("/api/preflight")
    def preflight(_admin: dict = admin_dep) -> dict:
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

    # -- brand logo ----------------------------------------------------------------
    @app.get("/api/logo")
    def get_logo(user: dict = user_dep, s: Services = svc_dep) -> dict:
        return {"logo": _logo_meta(s, user["id"])}

    @app.get("/api/logo/image")
    def get_logo_image(user: dict = user_dep, s: Services = svc_dep):
        if not s.storage.exists(logo_key(user["id"])):
            raise HTTPException(404, "No logo uploaded yet")
        # Browsers must check again each time, so a replaced logo is never shown from an old copy.
        return FileResponse(s.storage.local_path(logo_key(user["id"])), media_type="image/png",
                            headers={"Cache-Control": "no-cache"})

    @app.post("/api/logo", status_code=201)
    def upload_logo(file: UploadFile = File(...), user: dict = user_dep, s: Services = svc_dep) -> dict:
        if s.ffmpeg is None:
            raise HTTPException(503, "FFmpeg is not available on this server")
        raw = file.file.read(LOGO_MAX_BYTES + 1)
        if len(raw) > LOGO_MAX_BYTES:
            raise HTTPException(413, "The logo must be 5 MB or smaller")
        try:
            ext = check_logo_file(file.filename or "", raw[:16])
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        work = settings.work_dir / f"logo-{new_id()}"
        work.mkdir(parents=True, exist_ok=True)
        try:
            src = work / f"upload{ext}"
            src.write_bytes(raw)
            png = work / "logo.png"
            try:
                s.ffmpeg.image_to_png(src, png)
            except MediaError:
                raise HTTPException(400, "That image could not be read. Use a PNG, JPG or WebP file.")
            s.storage.put_file(logo_key(user["id"]), png)
        finally:
            shutil.rmtree(work, ignore_errors=True)
        meta = dumps({"name": safe_filename(file.filename or "logo")[:120], "uploaded_at": now_iso()})
        s.db.run(
            "INSERT INTO settings (user_id, key, value) VALUES (?, 'logo_meta', ?) "
            "ON CONFLICT (user_id, key) DO UPDATE SET value = excluded.value",
            (user["id"], meta),
        )
        return {"logo": _logo_meta(s, user["id"])}

    @app.delete("/api/logo")
    def delete_logo(user: dict = user_dep, s: Services = svc_dep) -> dict:
        s.storage.delete(logo_key(user["id"]))
        s.db.run("DELETE FROM settings WHERE user_id = ? AND key = 'logo_meta'", (user["id"],))
        return {"deleted": True}

    # -- transcription models ----------------------------------------------------
    @app.get("/api/models")
    def models(s: Services = svc_dep) -> dict:
        from .transcription.local import MODEL_SIZES, model_status

        return {"models": [model_status(settings.models_dir, n) for n in MODEL_SIZES],
                "active": settings.whisper_model}

    @app.post("/api/models/{name}/download", status_code=202)
    def download_model(name: str, _admin: dict = admin_dep, s: Services = svc_dep) -> dict:
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
    def list_effects(user: dict = user_dep, s: Services = svc_dep) -> dict:
        folders = library.theme_folder_names(settings)
        themes = []
        for name in effects.list_themes(s):
            rows = s.db.all("SELECT id, kind, file_name, duration, tags_json, license FROM effects WHERE theme = ? ORDER BY id", (name,))
            themes.append({
                "name": name,
                "folder": folders.get(name),
                "count": len(rows),
                "effects": [
                    {"id": r["id"], "kind": r["kind"], "file": r["file_name"], "duration": r["duration"],
                     "tags": loads(r["tags_json"], []), "license": r["license"],
                     "url": _media_link(s, user["id"], f"effect:{r['id']}", f"/api/effects/{r['id']}/file")}
                    for r in rows
                ],
            })
        return {"themes": themes, "max_effects_per_clip": settings.max_effects_per_clip}

    @app.get("/api/effects/{effect_id:path}/file")
    def effect_file(effect_id: str, user: dict = effect_media_dep, s: Services = svc_dep):
        row = s.db.one("SELECT * FROM effects WHERE id = ?", (effect_id,))
        if row is None:
            raise HTTPException(404, "Not found")
        local = settings.effects_dir.joinpath(*row["storage_key"].split("/")[1:])
        if not local.is_file():
            try:
                local = s.storage.local_path(row["storage_key"])
            except FileNotFoundError:
                raise HTTPException(404, "The file is missing")
        return FileResponse(local, filename=Path(row["file_name"]).name)

    # -- library management (admin, from the website) ------------------------------
    @app.post("/api/admin/themes", status_code=201)
    def admin_create_theme(body: ThemeIn, _admin: dict = admin_dep, s: Services = svc_dep) -> dict:
        try:
            theme = library.create_theme(settings, body.name, body.description)
        except library.LibraryError as exc:
            raise HTTPException(400, str(exc))
        effects.rescan(s)
        return theme

    @app.delete("/api/admin/themes/{folder}")
    def admin_delete_theme(folder: str, _admin: dict = admin_dep, s: Services = svc_dep) -> dict:
        try:
            library.delete_theme(settings, folder)
        except library.LibraryError as exc:
            raise HTTPException(404, str(exc))
        effects.rescan(s)
        return {"deleted": True}

    @app.post("/api/admin/themes/{folder}/effects", status_code=201)
    def admin_add_effect(
        folder: str,
        file: UploadFile = File(...),
        kind: str = Form(...),
        name: str = Form(""),
        tags: str = Form(""),
        license: str = Form(""),
        source: str = Form(""),
        speech: bool = Form(False),
        _admin: dict = admin_dep,
        s: Services = svc_dep,
    ) -> dict:
        data = file.file.read(library.MAX_UPLOAD_BYTES + 1)
        try:
            entry = library.add_effect(
                settings, folder, file.filename or "", data, kind=kind, name=name,
                tags=[t for t in tags.split(",")], license_text=license, source=source, speech=speech,
                probe=library.find_ffmpeg_probe(s),
            )
        except library.LibraryError as exc:
            raise HTTPException(400, str(exc))
        effects.rescan(s)
        return entry

    @app.delete("/api/admin/themes/{folder}/effects")
    def admin_remove_effect(folder: str, file: str, _admin: dict = admin_dep, s: Services = svc_dep) -> dict:
        try:
            library.remove_effect(settings, folder, file)
        except library.LibraryError as exc:
            raise HTTPException(404, str(exc))
        effects.rescan(s)
        return {"deleted": True}

    @app.post("/api/effects/rescan")
    def rescan_effects(_admin: dict = admin_dep, s: Services = svc_dep) -> dict:
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
        if body.verify:
            probe = {
                "id": "probe", "preset": body.preset, "kind": body.kind, "base_url": base_url or None,
                "model": model, "api_key_enc": encrypt_secret(settings, body.api_key) if body.api_key else None,
            }
            try:
                if body.kind == "transcription":
                    check_transcription_provider(settings, probe)
                else:
                    build_llm_provider(settings, probe).test()
            except Exception as exc:
                raise HTTPException(400, f"The provider did not accept a test request, so it was not saved: {exc}")
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
        batch_id: Optional[str] = Form(None),
        user: dict = user_dep,
        s: Services = svc_dep,
    ) -> dict:
        if batch_id:
            _owned(s, "batches", batch_id, user)
        today_count = s.db.one(
            "SELECT COUNT(*) AS n FROM projects WHERE user_id = ? AND created_at >= ?",
            (user["id"], today()),
        )["n"]
        if today_count >= settings.max_projects_per_user_per_day:
            raise HTTPException(429, "Daily project limit reached")
        used_mb = (s.db.one("SELECT COALESCE(SUM(file_size), 0) AS n FROM projects WHERE user_id = ?",
                            (user["id"],))["n"] or 0) / 1048576
        if used_mb >= settings.user_storage_quota_mb:
            raise HTTPException(413, "Your storage quota is full. Delete old projects to free space.")
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
            if is_platform_link(url) and not settings.allow_platform_links:
                raise HTTPException(400, "YouTube links are turned off on this server. Upload the video file instead.")
            try:
                validate_link(url or "")
            except SecurityError as exc:
                raise HTTPException(400, str(exc))
            source_type, location, source_key, file_size = "link", url.strip(), None, None

        display = (title or "").strip() or (Path(location).stem if source_type == "upload" else location[:80])
        mode_row = s.db.one("SELECT value FROM settings WHERE user_id = ? AND key = 'transcription_mode'", (user["id"],))
        transcription_mode = loads(mode_row["value"]) if mode_row else settings.default_transcription_engine
        s.db.run(
            "INSERT INTO projects (id, user_id, batch_id, title, source_type, source_location, source_key, file_size, "
            "mode, status, progress, status_message, transcription_mode, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', 0, 'Waiting to start', ?, ?, ?)",
            (project_id, user["id"], batch_id or None, display, source_type, location, source_key, file_size,
             settings.mode, transcription_mode, now, now),
        )
        s.queue.enqueue(user["id"], project_id, "download")
        return _project_view(s, s.db.one("SELECT * FROM projects WHERE id = ?", (project_id,)))

    @app.get("/api/queue")
    def queue_status(user: dict = user_dep, s: Services = svc_dep) -> dict:
        return server_load(s)

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
        try:
            start_generation(s, row, body.model_dump())
        except ValueError as exc:
            raise HTTPException(400, str(exc))
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
    def clip_video(clip_id: str, user: dict = clip_media_dep, s: Services = svc_dep):
        clip = _clip_owned(s, clip_id, user)
        return _serve_clip(s, clip, attachment=False)

    @app.get("/api/clips/{clip_id}/download")
    def clip_download(clip_id: str, user: dict = clip_media_dep, s: Services = svc_dep):
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
    def project_zip(project_id: str, user: dict = project_media_dep, s: Services = svc_dep):
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

    # -- performance and analytics -------------------------------------------------
    @app.post("/api/clips/{clip_id}/metrics")
    def clip_metrics(clip_id: str, body: MetricsBody, user: dict = user_dep, s: Services = svc_dep) -> dict:
        _clip_owned(s, clip_id, user)
        s.db.run("UPDATE clips SET views = ?, likes = ?, updated_at = ? WHERE id = ?",
                 (body.views, body.likes, now_iso(), clip_id))
        return {"saved": True}

    @app.get("/api/analytics")
    def analytics(user: dict = user_dep, s: Services = svc_dep) -> dict:
        return _analytics(s, user["id"])

    # -- batches -----------------------------------------------------------------------
    @app.post("/api/batches", status_code=201)
    def create_batch(body: BatchIn, user: dict = user_dep, s: Services = svc_dep) -> dict:
        name = body.name.strip()
        if not name:
            raise HTTPException(400, "Give the batch a name")
        batch_id = new_id()
        s.db.run("INSERT INTO batches (id, user_id, name, settings_json, created_at) VALUES (?, ?, ?, NULL, ?)",
                 (batch_id, user["id"], name[:120], now_iso()))
        return _batch_view(s, _owned(s, "batches", batch_id, user))

    @app.get("/api/batches")
    def list_batches(user: dict = user_dep, s: Services = svc_dep) -> dict:
        rows = s.db.all("SELECT * FROM batches WHERE user_id = ? ORDER BY created_at DESC", (user["id"],))
        return {"batches": [_batch_view(s, r) for r in rows]}

    @app.get("/api/batches/{batch_id}")
    def get_batch(batch_id: str, user: dict = user_dep, s: Services = svc_dep) -> dict:
        return _batch_view(s, _owned(s, "batches", batch_id, user), with_projects=True)

    @app.put("/api/batches/{batch_id}/settings")
    def batch_settings(batch_id: str, body: GenerateBody, user: dict = user_dep, s: Services = svc_dep) -> dict:
        """Save the clip settings for the batch, then start generating every project that is
        ready. Projects still transcribing start automatically when their transcript is done."""
        batch = _owned(s, "batches", batch_id, user)
        opts = body.model_dump()
        applied, waiting, errors = 0, 0, []
        projects = s.db.all("SELECT * FROM projects WHERE batch_id = ? AND user_id = ?", (batch_id, user["id"]))
        for p in projects:
            ready = s.db.one("SELECT 1 AS x FROM transcripts WHERE project_id = ?", (p["id"],)) is not None
            if p["status"] in ACTIVE_STATES or not ready:
                waiting += 1
                continue
            try:
                start_generation(s, p, opts)
                applied += 1
            except ValueError as exc:
                errors.append(f"{p['title']}: {exc}")
        s.db.run("UPDATE batches SET settings_json = ? WHERE id = ?", (dumps(opts), batch["id"]))
        return {"applied": applied, "waiting": waiting, "errors": errors}

    @app.delete("/api/batches/{batch_id}")
    def delete_batch(batch_id: str, user: dict = user_dep, s: Services = svc_dep) -> dict:
        _owned(s, "batches", batch_id, user)
        s.db.run_many([
            ("UPDATE projects SET batch_id = NULL WHERE batch_id = ?", (batch_id,)),
            ("DELETE FROM batches WHERE id = ?", (batch_id,)),
        ])
        return {"deleted": True}

    # -- YouTube cookies (admin) -----------------------------------------------------
    @app.get("/api/admin/youtube-cookies")
    def youtube_cookies_status(_admin: dict = admin_dep) -> dict:
        return _cookies_status(settings)

    @app.post("/api/admin/youtube-cookies", status_code=201)
    def upload_youtube_cookies(file: UploadFile = File(...), _admin: dict = admin_dep) -> dict:
        raw = file.file.read(MAX_COOKIES_BYTES + 1)
        if len(raw) > MAX_COOKIES_BYTES:
            raise HTTPException(413, "That file is too large to be a cookies file")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise HTTPException(400, "This does not look like a text cookies file")
        lines = [ln for ln in text.splitlines() if ln.strip()]
        if not lines or not lines[0].startswith("# ") or "Cookie File" not in lines[0]:
            raise HTTPException(400, "Use a cookies.txt file (Netscape format), exported from your browser")
        youtube = [ln for ln in lines if not ln.startswith("#") and "youtube.com" in ln]
        if not youtube:
            raise HTTPException(400, "The file has no YouTube cookies. Export it while signed in to YouTube.")
        target = settings.youtube_cookies_file
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_bytes(raw)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        tmp.replace(target)
        return _cookies_status(settings)

    @app.delete("/api/admin/youtube-cookies")
    def delete_youtube_cookies(_admin: dict = admin_dep) -> dict:
        settings.youtube_cookies_file.unlink(missing_ok=True)
        return _cookies_status(settings)

    # -- interface ---------------------------------------------------------------
    web_dir = ROOT / "web"
    if web_dir.exists():
        app.mount("/", StaticFiles(directory=str(web_dir), html=True), name="web")

    return app


# -- helpers ---------------------------------------------------------------------

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _clean_email(value: str) -> str:
    email = (value or "").strip().lower()
    if not _EMAIL_RE.match(email):
        raise HTTPException(400, "Enter a valid email address")
    return email


def _start_session(s: Services, user_id: str, email: str, role: str, response: Response) -> dict:
    """Issue a short-lived access token in the body, and a rotating refresh token in an
    HttpOnly cookie that only the /api/auth routes can read."""
    refresh = issue_refresh_token(s.db, user_id)
    response.set_cookie(
        "clipper_refresh", refresh, httponly=True, samesite="lax", secure=s.settings.cookie_secure,
        path="/api/auth", max_age=30 * 24 * 3600,
    )
    access = issue_access_token(s.settings, {"id": user_id, "role": role})
    return {"access_token": access, "expires_in": 900, "user": {"id": user_id, "email": email, "role": role}}


def _batch_view(svc: Services, row: dict, with_projects: bool = False) -> dict:
    counts = {r["status"]: r["n"] for r in svc.db.all(
        "SELECT status, COUNT(*) AS n FROM projects WHERE batch_id = ? GROUP BY status", (row["id"],))}
    view = {"id": row["id"], "name": row["name"], "created_at": row["created_at"],
            "settings": loads(row.get("settings_json"), None), "projects": sum(counts.values()),
            "status_counts": counts}
    if with_projects:
        rows = svc.db.all("SELECT * FROM projects WHERE batch_id = ? ORDER BY created_at", (row["id"],))
        view["items"] = [_project_view(svc, r, with_clips=False) for r in rows]
    return view


def _score_band(score) -> str:
    value = score or 0.0
    if value < 0.5:
        return "below 0.5"
    if value < 0.7:
        return "0.5-0.7"
    if value < 0.85:
        return "0.7-0.85"
    return "0.85 and up"


def _length_band(seconds: float) -> str:
    if seconds <= 30:
        return "30 s or less"
    if seconds <= 45:
        return "31-45 s"
    if seconds <= 60:
        return "46-60 s"
    return "over 60 s"


def _analytics(svc: Services, user_id: str) -> dict:
    rows = svc.db.all(
        "SELECT c.id, c.score, c.reason, c.views, c.likes, c.start_sec, c.end_sec, c.effects_json, "
        "p.title, p.theme FROM clips c JOIN projects p ON p.id = c.project_id "
        "WHERE p.user_id = ? AND c.status = 'done'", (user_id,))
    measured = [r for r in rows if r["views"] is not None]

    def summarise(items: list) -> dict:
        if not items:
            return {"clips": 0, "avg_views": None, "avg_likes": None}
        return {
            "clips": len(items),
            "avg_views": round(sum(r["views"] for r in items) / len(items), 1),
            "avg_likes": round(sum((r["likes"] or 0) for r in items) / len(items), 1),
        }

    def group(key_fn, labels: list) -> dict:
        buckets = {label: [] for label in labels}
        for r in measured:
            buckets.setdefault(key_fn(r), []).append(r)
        return {label: summarise(items) for label, items in buckets.items()}

    has_effects = lambda r: bool(loads(r["effects_json"], []))  # noqa: E731
    top = sorted(measured, key=lambda r: r["views"], reverse=True)[:5]
    return {
        "clips_total": len(rows),
        "clips_with_metrics": len(measured),
        "overall": summarise(measured),
        "by_score": group(lambda r: _score_band(r["score"]), ["below 0.5", "0.5-0.7", "0.7-0.85", "0.85 and up"]),
        "by_length": group(lambda r: _length_band((r["end_sec"] or 0) - (r["start_sec"] or 0)),
                           ["30 s or less", "31-45 s", "46-60 s", "over 60 s"]),
        "by_theme": group(lambda r: r["theme"] or "unknown", sorted({r["theme"] or "unknown" for r in measured})),
        "with_effects": summarise([r for r in measured if has_effects(r)]),
        "without_effects": summarise([r for r in measured if not has_effects(r)]),
        "top_clips": [{"id": r["id"], "title": r["title"], "reason": r["reason"], "score": r["score"],
                       "views": r["views"], "likes": r["likes"]} for r in top],
    }


MAX_COOKIES_BYTES = 1024 * 1024


def _cookies_status(settings: Settings) -> dict:
    """Shows that a file is saved and how many YouTube cookies it holds. Never shows the contents."""
    path = settings.youtube_cookies_file
    if not path.is_file():
        return {"saved": False, "youtube_cookies": 0, "saved_at": None}
    count = 0
    for ln in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if ln and not ln.startswith("#") and "youtube.com" in ln:
            count += 1
    saved_at = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")
    return {"saved": True, "youtube_cookies": count, "saved_at": saved_at}


def _admin_count(s: Services) -> int:
    return s.db.one("SELECT COUNT(*) AS n FROM users WHERE role = 'admin' AND disabled = 0")["n"]


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
    if key == "default_moments_per_clip" and (not isinstance(value, int) or not 1 <= value <= MOMENTS_MAX):
        raise HTTPException(400, f"Moments per clip must be between 1 and {MOMENTS_MAX}")
    if key == "default_logo_on" and not isinstance(value, bool):
        raise HTTPException(400, "The logo setting must be on or off")
    bounds = {"default_logo_opacity": (0, 100), "default_logo_size": (5, 50),
              "default_logo_x": (0, 100), "default_logo_y": (0, 100)}
    if key in bounds:
        lo, hi = bounds[key]
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not lo <= value <= hi:
            raise HTTPException(400, f"The logo setting must be between {lo} and {hi}")


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
    url = svc.storage.download_url(clip["output_key"], f"clip_{clip['idx']:02d}.mp4", attachment=attachment)
    if url:
        return RedirectResponse(url)
    path = svc.storage.local_path(clip["output_key"])
    filename = f"clip_{clip['idx']:02d}.mp4"
    if attachment:
        return FileResponse(path, media_type="video/mp4", filename=filename)
    return FileResponse(path, media_type="video/mp4", headers={"Content-Disposition": f'inline; filename="{filename}"'})


def check_transcription_provider(settings: Settings, row: dict) -> str:
    """Check the key with a cheap authenticated request that does not use any audio."""
    if row["preset"] == "aws_transcribe":
        try:
            import boto3

            boto3.client("transcribe", region_name=settings.s3_region).list_transcription_jobs(MaxResults=1)
        except Exception as exc:
            raise RuntimeError(f"AWS Transcribe is not reachable: {exc}") from exc
        return "Connection OK (AWS credentials accepted)"
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


def _media_link(svc: Services, user_id: str, ref: str, path: str) -> str:
    return f"{path}?t={issue_media_token(svc.settings, user_id, ref)}"


def _logo_meta(svc: Services, user_id: str) -> Optional[dict]:
    """The uploaded logo's name and date, or None when there is no logo."""
    if not svc.storage.exists(logo_key(user_id)):
        return None
    row = svc.db.one("SELECT value FROM settings WHERE user_id = ? AND key = 'logo_meta'", (user_id,))
    meta = loads(row["value"], {}) if row else {}
    return {"name": meta.get("name", "logo"), "uploaded_at": meta.get("uploaded_at")}


def _clip_rows(svc: Services, project_id: str) -> list[dict]:
    rows = svc.db.all("SELECT * FROM clips WHERE project_id = ? ORDER BY idx", (project_id,))
    owner = svc.db.one("SELECT user_id FROM projects WHERE id = ?", (project_id,))["user_id"]
    out = []
    for r in rows:
        parts = clip_parts(r["start_sec"], r["end_sec"], r.get("segments_json"))
        out.append({
            "id": r["id"], "idx": r["idx"], "start": r["start_sec"], "end": r["end_sec"],
            "length": round(sum(b - a for a, b in parts), 2),
            "moments": len(parts),
            "segments": [[round(a, 2), round(b, 2)] for a, b in parts],
            "score": r["score"], "reason": r["reason"], "status": r["status"], "error": r["error"],
            "provider_used": r["provider_used"], "settings": loads(r["settings_json"], {}),
            "effects": loads(r["effects_json"], []), "views": r["views"], "likes": r["likes"],
            "video_url": _media_link(svc, owner, f"clip:{r['id']}", f"/api/clips/{r['id']}/video") if r["status"] == "done" else None,
            "download_url": _media_link(svc, owner, f"clip:{r['id']}", f"/api/clips/{r['id']}/download") if r["status"] == "done" else None,
        })
    return out


def _project_view(svc: Services, row: dict, with_clips: bool = True) -> dict:
    view = {
        "id": row["id"],
        "title": row["title"],
        "batch_id": row.get("batch_id"),
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
        "effects_enabled": bool(row.get("effects_enabled", 1)),
        "captions_style": row["captions_style"],
        "orientation": row["orientation"],
        "resolution": row["resolution"],
        "moments_per_clip": row.get("moments_per_clip") or 1,
        "logo": loads(row.get("logo_json"), None),
        "transcription_engine": row["transcription_engine"],
        "provider_used": row["provider_used"],
        "error": row["error"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "has_transcript": svc.db.one("SELECT 1 AS x FROM transcripts WHERE project_id = ?", (row["id"],)) is not None,
        "zip_url": _media_link(svc, row["user_id"], f"project:{row['id']}", f"/api/projects/{row['id']}/zip"),
        # Where this project waits in line, when its next step is queued behind other work.
        "queue": queue_info(svc, row["id"]) if row["status"] in ACTIVE_STATES else None,
    }
    if with_clips:
        view["clips"] = _clip_rows(svc, row["id"])
    return view
