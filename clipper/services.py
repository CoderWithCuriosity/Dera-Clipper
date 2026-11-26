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

    def record_usage(self, provider_id: str, tokens: int = 0, failed: bool = False) -> None:
        from .db import today

        self.db.run(
            "INSERT INTO provider_usage (provider_id, day, requests, tokens, failures) VALUES (?, ?, 1, ?, ?) "
            "ON CONFLICT (provider_id, day) DO UPDATE SET requests = provider_usage.requests + 1, "
            "tokens = provider_usage.tokens + excluded.tokens, failures = provider_usage.failures + excluded.failures",
            (provider_id, today(), int(tokens), 1 if failed else 0),
        )


def build_llm_provider(settings: Settings, row: dict) -> LLMProvider:
    preset = PRESETS.get(row["preset"])
    if preset is None:
        raise ValueError(f"unknown preset {row['preset']}")
    api_key = decrypt_secret(settings, row["api_key_enc"]) if row.get("api_key_enc") else None
    base_url = row.get("base_url") or preset["base_url"]
    model = row.get("model") or preset["model"]
    return OpenAICompatible(name=row["id"], base_url=base_url, model=model, api_key=api_key)


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
        db = self.svc.db
        candidate = db.one(
            "SELECT id FROM jobs WHERE status = 'queued' AND run_after <= ? ORDER BY created_at LIMIT 1",
            (now_iso(),),
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
