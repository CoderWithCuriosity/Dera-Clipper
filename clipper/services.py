"""Shared services: database, storage, media engine, the job queue, and the provider registry."""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Optional

from .config import Settings
from .db import Database, dumps, iso_in, loads, new_id, now_iso
from .llm import PRESETS, LLMProvider
from .llm.openai_compat import OpenAICompatible
from .media import Ffmpeg, find_ffmpeg
from .security import decrypt_secret
from .storage import Storage, build_storage

log = logging.getLogger("clipper")

JOB_TYPES = ("download", "transcribe", "plan", "render")


@dataclass
class Services:
    settings: Settings
    db: Database
    storage: Storage
    ffmpeg: Optional[Ffmpeg]
    queue: "JobQueue"

    @classmethod
    def create(cls, settings: Settings) -> "Services":
        db = Database(settings.database_url)
        storage = build_storage(settings)
        binary = find_ffmpeg(settings)
        ffmpeg = Ffmpeg(binary) if binary else None
        services = cls(settings=settings, db=db, storage=storage, ffmpeg=ffmpeg, queue=None)  # type: ignore[arg-type]
        services.queue = JobQueue(services)
        return services

    def require_ffmpeg(self) -> Ffmpeg:
        if self.ffmpeg is None:
            raise RuntimeError("FFmpeg was not found. Install it, or run: pip install imageio-ffmpeg")
        return self.ffmpeg

    # -- projects -----------------------------------------------------------------
    def set_project(self, project_id: str, **fields) -> None:
        fields["updated_at"] = now_iso()
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.db.run(f"UPDATE projects SET {cols} WHERE id = ?", (*fields.values(), project_id))

    # -- providers ----------------------------------------------------------------
    def llm_providers(self, user_id: str) -> list[LLMProvider]:
        """Enabled language-model providers for a user, in priority order."""
        rows = self.db.all(
            "SELECT * FROM providers WHERE user_id = ? AND kind = 'llm' AND enabled = 1 ORDER BY priority, created_at",
            (user_id,),
        )
        built: list[LLMProvider] = []
        for row in rows:
            try:
                built.append(build_llm_provider(self.settings, row))
            except Exception as exc:  # a broken row must not block the others
                log.warning("Skipping provider %s: %s", row["name"], exc)
        return built

    def record_usage(self, provider_id: str, tokens: int = 0, failed: bool = False,
                     rate_limit: Optional[dict] = None) -> None:
        from .db import today

        self.db.run(
            "INSERT INTO provider_usage (provider_id, day, requests, tokens, failures) VALUES (?, ?, 1, ?, ?) "
            "ON CONFLICT (provider_id, day) DO UPDATE SET requests = provider_usage.requests + 1, "
            "tokens = provider_usage.tokens + excluded.tokens, failures = provider_usage.failures + excluded.failures",
            (provider_id, today(), int(tokens), 1 if failed else 0),
        )
        if rate_limit:
            # Keep the limits the provider last reported, so the settings page can show them.
            self.db.run("UPDATE providers SET last_rate_limit = ? WHERE id = ?", (dumps(rate_limit), provider_id))

    def transcription_chain(self, user_id: str, mode: str) -> list:
        """Engines to try in order. Mode "local" uses only local Whisper. Mode "cloud" uses the
        enabled cloud providers by priority, and falls back to local Whisper if none work."""
        engines: list = []
        if mode == "cloud":
            rows = self.db.all(
                "SELECT * FROM providers WHERE user_id = ? AND kind = 'transcription' AND enabled = 1 "
                "ORDER BY priority, created_at", (user_id,),
            )
            for row in rows:
                try:
                    engines.append((row["id"], build_transcription_provider(self, row)))
                except Exception as exc:
                    log.warning("Skipping transcription provider %s: %s", row["name"], exc)
        from .transcription import build_local_engine

        engines.append(("local", build_local_engine(self.settings)))
        return engines


CLIP_STATES_ACTIVE = ("queued", "acquiring", "preparing_audio", "transcribing", "planning", "rendering")


def start_generation(svc: "Services", project: dict, opts: dict) -> None:
    """Store the clip settings on a project and queue planning. Raises ValueError with a
    message the user can act on. Used by the Generate button and by batches."""
    from . import effects
    from .media import ORIENTATIONS, RESOLUTIONS, caption_styles

    theme = opts.get("theme")
    if theme not in effects.list_themes(svc):
        raise ValueError("Unknown theme")
    try:
        clip_length = float(opts["clip_length"])
        clip_count = int(opts["clip_count"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Clip length and clip count are required") from exc
    if clip_length <= 0:
        raise ValueError("Clip length must be greater than zero")
    if project.get("duration") is None or clip_length > project["duration"]:
        raise ValueError(f"Clip length cannot exceed the video length ({(project.get('duration') or 0):.0f} s)")
    if not 1 <= clip_count <= 20:
        raise ValueError("Clip count must be between 1 and 20")
    captions = opts.get("captions_style", "classic")
    if captions not in set(caption_styles()) | {"off"}:
        raise ValueError("Unknown caption style")
    orientation = opts.get("orientation", "landscape")
    if orientation not in ORIENTATIONS:
        raise ValueError("Unknown orientation")
    resolution = opts.get("resolution", "1080p")
    if resolution not in RESOLUTIONS:
        raise ValueError("Unknown resolution")
    svc.set_project(
        project["id"], theme=theme, clip_length=clip_length, clip_count=clip_count,
        mirror=1 if opts.get("mirror") else 0, captions_style=captions, orientation=orientation,
        effects_enabled=1 if opts.get("effects", True) else 0,
        resolution=resolution, error=None, status="planning", status_message="Queued for planning", progress=0.0,
    )
    svc.queue.enqueue(project["user_id"], project["id"], "plan")


def build_llm_provider(settings: Settings, row: dict) -> LLMProvider:
    preset = PRESETS.get(row["preset"])
    if preset is None:
        raise ValueError(f"unknown preset {row['preset']}")
    api_key = decrypt_secret(settings, row["api_key_enc"]) if row.get("api_key_enc") else None
    base_url = row.get("base_url") or preset["base_url"]
    model = row.get("model") or preset["model"]
    if preset["kind"] == "anthropic":
        from .llm.anthropic import AnthropicProvider

        return AnthropicProvider(name=row["id"], base_url=base_url, model=model, api_key=api_key)
    return OpenAICompatible(name=row["id"], base_url=base_url, model=model, api_key=api_key)


def build_transcription_provider(svc: "Services", row: dict):
    from .transcription.cloud import TRANSCRIPTION_PRESETS, WhisperAPI

    preset = TRANSCRIPTION_PRESETS.get(row["preset"])
    if preset is None:
        raise ValueError(f"unknown transcription preset {row['preset']}")
    if row["preset"] == "aws_transcribe":
        from .transcription.aws import AWSTranscribe

        if svc.settings.storage != "s3" or not svc.settings.s3_bucket:
            raise ValueError("AWS Transcribe needs S3 storage (CLIPPER_STORAGE=s3 and a bucket)")
        return AWSTranscribe(storage=svc.storage, region=svc.settings.s3_region,
                             bucket=svc.settings.s3_bucket, prefix=svc.settings.s3_prefix)
    api_key = decrypt_secret(svc.settings, row["api_key_enc"]) if row.get("api_key_enc") else None
    return WhisperAPI(
        name=row["id"], base_url=row.get("base_url") or preset["base_url"],
        model=row.get("model") or preset["model"], api_key=api_key, ffmpeg=svc.require_ffmpeg(),
    )


class JobQueue:
    """Jobs live in the database. A worker claims one with a conditional update, so several
    workers (threads, or processes on other machines) can share the same queue safely."""

    def __init__(self, services: Services):
        self.svc = services
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()

    def enqueue(self, user_id: str, project_id: Optional[str], job_type: str,
                payload: Optional[dict] = None, delay: float = 0.0) -> str:
        if job_type not in JOB_TYPES:
            raise ValueError(f"unknown job type {job_type}")
        job_id = new_id()
        now = now_iso()
        self.svc.db.run(
            "INSERT INTO jobs (id, user_id, project_id, type, status, progress, attempts, payload_json, "
            "run_after, created_at, updated_at) VALUES (?, ?, ?, ?, 'queued', 0, 0, ?, ?, ?, ?)",
            (job_id, user_id, project_id, job_type, dumps(payload or {}), iso_in(delay), now, now),
        )
        return job_id

    def claim(self) -> Optional[dict]:
        """Take the oldest queued job whose owner is still under their concurrency limit, so one
        heavy user cannot hold every worker."""
        db = self.svc.db
        candidate = db.one(
            "SELECT j.id FROM jobs j WHERE j.status = 'queued' AND j.run_after <= ? AND "
            "(SELECT COUNT(*) FROM jobs r WHERE r.user_id = j.user_id AND r.status = 'running') < ? "
            "ORDER BY j.created_at LIMIT 1",
            (now_iso(), self.svc.settings.max_running_jobs_per_user),
        )
        if not candidate:
            return None
        claimed = db.run(
            "UPDATE jobs SET status = 'running', attempts = attempts + 1, updated_at = ? "
            "WHERE id = ? AND status = 'queued'",
            (now_iso(), candidate["id"]),
        )
        if claimed != 1:
            return None
        job = db.one("SELECT * FROM jobs WHERE id = ?", (candidate["id"],))
        job["payload"] = loads(job.get("payload_json"), {})
        return job

    def finish(self, job_id: str, error: Optional[str] = None) -> None:
        self.svc.db.run(
            "UPDATE jobs SET status = ?, error = ?, progress = ?, updated_at = ? WHERE id = ?",
            ("failed" if error else "done", error, 1.0 if not error else 0.0, now_iso(), job_id),
        )

    def requeue(self, job_id: str, delay: float, error: str) -> None:
        self.svc.db.run(
            "UPDATE jobs SET status = 'queued', error = ?, run_after = ?, updated_at = ? WHERE id = ?",
            (error, iso_in(delay), now_iso(), job_id),
        )

    def recover_interrupted(self) -> int:
        """Jobs left 'running' by a crashed process go back to the queue."""
        return self.svc.db.run(
            "UPDATE jobs SET status = 'queued', run_after = ? WHERE status = 'running'", (now_iso(),)
        )

    # -- worker threads -------------------------------------------------------------
    def start(self, handlers: dict, concurrency: int) -> None:
        self.recover_interrupted()
        for n in range(max(1, concurrency)):
            t = threading.Thread(target=self._loop, args=(handlers,), name=f"clipper-worker-{n}", daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()

    def _loop(self, handlers: dict) -> None:
        from .pipeline import TransientError

        while not self._stop.is_set():
            job = self.claim()
            if job is None:
                time.sleep(1.0)
                continue
            handler = handlers.get(job["type"])
            try:
                if handler is None:
                    raise RuntimeError(f"no handler for {job['type']}")
                handler(job)
                self.finish(job["id"])
            except TransientError as exc:
                if job["attempts"] < self.svc.settings.job_max_attempts:
                    delay = 15.0 * (2 ** (job["attempts"] - 1))
                    self.requeue(job["id"], delay, str(exc))
                    log.info("Job %s will retry in %.0fs: %s", job["id"], delay, exc)
                else:
                    self._fail(job, str(exc))
            except Exception as exc:  # media and configuration errors are not retried
                log.exception("Job %s failed", job["id"])
                self._fail(job, str(exc))

    def _fail(self, job: dict, message: str) -> None:
        self.finish(job["id"], error=message)
        if job.get("project_id") and job["type"] != "render":
            self.svc.set_project(job["project_id"], status="failed", error=message,
                                 status_message="Something went wrong")
