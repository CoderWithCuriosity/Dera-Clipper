"""Shared services: database, storage, media engine, the job queue, and the provider registry."""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Optional

from .branding import MOMENTS_MAX, MOMENTS_MIN_SECONDS, clean_logo, logo_key
from .config import Settings
from .db import Database, dumps, iso_in, loads, new_id, now_iso
from .llm import PRESETS, LLMProvider
from .llm.openai_compat import OpenAICompatible
from .media import Ffmpeg, find_ffmpeg
from .security import decrypt_secret
from .storage import Storage, build_storage

log = logging.getLogger("clipper")

JOB_TYPES = ("download", "transcribe", "plan", "render")
HEAVY_TYPES = ("transcribe", "render")
MIN_FREE_MEMORY_MB = 900  # a heavy job needs about this much; below it, the job waits


def memory_has_room() -> bool:
    """False when the server is short of memory. Linux only; elsewhere it always allows work."""
    try:
        with open("/proc/meminfo", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024 >= MIN_FREE_MEMORY_MB
    except (OSError, ValueError):
        pass
    return True


def queue_info(svc: "Services", project_id: str) -> Optional[dict]:
    """Where a waiting project stands in line. None when its work is not waiting."""
    job = svc.db.one(
        "SELECT * FROM jobs WHERE project_id = ? AND status = 'queued' ORDER BY created_at LIMIT 1",
        (project_id,),
    )
    if job is None:
        return None
    ahead = svc.db.one(
        "SELECT COUNT(*) AS n FROM jobs WHERE status = 'queued' AND created_at < ?", (job["created_at"],),
    )["n"]
    heavy_busy = svc.db.one(
        "SELECT COUNT(*) AS n FROM jobs WHERE status = 'running' AND type IN ('transcribe', 'render')",
    )["n"]
    heavy = job["type"] in HEAVY_TYPES
    if heavy and heavy_busy >= svc.settings.heavy_jobs_max:
        reason = (f"The server works on {svc.settings.heavy_jobs_max} video at a time for heavy steps "
                  "(transcribing and rendering), so it stays responsive.")
    elif heavy and not memory_has_room():
        reason = "The server is short of memory right now. This starts as soon as memory frees up."
    else:
        reason = "Starting soon."
    return {"position": ahead + 1, "ahead": ahead, "heavy": heavy, "reason": reason}


def server_load(svc: "Services") -> dict:
    """What the queue is doing right now, for the banner on the projects page."""
    counts = {r["type"] + ":" + r["status"]: r["n"] for r in svc.db.all(
        "SELECT type, status, COUNT(*) AS n FROM jobs WHERE status IN ('running', 'queued') GROUP BY type, status")}
    heavy_running = sum(counts.get(f"{t}:running", 0) for t in HEAVY_TYPES)
    waiting = sum(v for k, v in counts.items() if k.endswith(":queued"))
    return {
        "heavy_running": heavy_running, "heavy_limit": svc.settings.heavy_jobs_max,
        "waiting": waiting, "memory_ok": memory_has_room(),
    }


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
    try:
        per_clip = int(opts.get("moments_per_clip") or 1)
        fewest = int(opts.get("moments_min") or per_clip)
    except (TypeError, ValueError) as exc:
        raise ValueError("Moments per clip must be whole numbers") from exc
    if not 1 <= per_clip <= MOMENTS_MAX or not 1 <= fewest <= MOMENTS_MAX:
        raise ValueError(f"Moments per clip must be between 1 and {MOMENTS_MAX}")
    if fewest > per_clip:
        raise ValueError("The fewest moments cannot be more than the most moments")
    if per_clip > 1 and fewest < 2:
        raise ValueError("A mixed clip needs at least 2 moments. Use Single moments for one moment per clip")
    if per_clip == 1:
        fewest = 1
    if clip_count < 1:
        raise ValueError("Clip count must be at least 1")
    if clip_length / per_clip < MOMENTS_MIN_SECONDS:
        raise ValueError(
            f"With up to {per_clip} moments per clip, each moment would be shorter than "
            f"{MOMENTS_MIN_SECONDS:.0f} seconds. Use a longer clip length or fewer moments"
        )
    logo = clean_logo(opts.get("logo"))
    if logo["enabled"] and not svc.storage.exists(logo_key(project["user_id"])):
        raise ValueError("Upload your logo in Setup before adding it to clips")
    from .branding import bumper_key

    for kind in ("intro", "outro"):
        if opts.get(kind) and not svc.storage.exists(bumper_key(project["user_id"], kind)):
            raise ValueError(f"Upload your {kind} video in Setup before adding it to clips")
    svc.set_project(
        project["id"], theme=theme, clip_length=clip_length, clip_count=clip_count,
        mirror=1 if opts.get("mirror") else 0, captions_style=captions, orientation=orientation,
        effects_enabled=1 if opts.get("effects", True) else 0,
        resolution=resolution, moments_per_clip=per_clip, moments_min=fewest, logo_json=dumps(logo),
        extras_json=dumps({"intro": bool(opts.get("intro")), "outro": bool(opts.get("outro"))}),
        error=None, status="planning", status_message="Queued for planning", progress=0.0,
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
        """Take the oldest queued job that may run now. Heavy jobs (transcribing and rendering)
        share a few slots, so the server's CPU and memory are never swamped. Light jobs have
        their own slots and keep moving while heavy ones wait. Each user is also capped, so one
        person cannot take every slot."""
        db = self.svc.db
        settings = self.svc.settings
        running = db.all("SELECT type, user_id FROM jobs WHERE status = 'running'")
        heavy_running = sum(1 for r in running if r["type"] in HEAVY_TYPES)
        light_running = len(running) - heavy_running
        per_user: dict[str, int] = {}
        for r in running:
            per_user[r["user_id"]] = per_user.get(r["user_id"], 0) + 1
        heavy_free = heavy_running < settings.heavy_jobs_max and memory_has_room()
        light_free = light_running < settings.light_jobs_max

        candidates = db.all(
            "SELECT id, user_id, type FROM jobs WHERE status = 'queued' AND run_after <= ? "
            "ORDER BY created_at LIMIT 100",
            (now_iso(),),
        )
        for cand in candidates:
            if per_user.get(cand["user_id"], 0) >= settings.max_running_jobs_per_user:
                continue
            if cand["type"] in HEAVY_TYPES and not heavy_free:
                continue
            if cand["type"] not in HEAVY_TYPES and not light_free:
                continue
            claimed = db.run(
                "UPDATE jobs SET status = 'running', attempts = attempts + 1, updated_at = ? "
                "WHERE id = ? AND status = 'queued'",
                (now_iso(), cand["id"]),
            )
            if claimed != 1:
                continue  # another thread took it first
            job = db.one("SELECT * FROM jobs WHERE id = ?", (cand["id"],))
            job["payload"] = loads(job.get("payload_json"), {})
            return job
        return None

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
