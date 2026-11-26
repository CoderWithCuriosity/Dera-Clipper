"""Background housekeeping: remove expired uploads and stale work folders, and take scheduled
backups. Runs in its own thread next to the job workers."""
from __future__ import annotations

import logging
import shutil
import threading
import time
from datetime import datetime, timedelta, timezone

from .services import Services

log = logging.getLogger("clipper.maintenance")
INTERVAL_SECONDS = 3600


def cleanup_expired_uploads(svc: Services) -> int:
    """Raw uploads are kept for a set number of days, then removed. Clips are kept."""
    days = svc.settings.upload_retention_days
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    rows = svc.db.all(
        "SELECT id FROM projects WHERE source_key IS NOT NULL AND status IN ('done', 'failed') AND updated_at < ?",
        (cutoff,),
    )
    for row in rows:
        svc.storage.delete_prefix(f"uploads/{row['id']}")
        svc.db.run("UPDATE projects SET source_key = NULL WHERE id = ?", (row["id"],))
    return len(rows)


def cleanup_stale_work(svc: Services, max_age_hours: int = 24) -> int:
    """Remove work folders left behind by crashed jobs."""
    root = svc.settings.work_dir
    if not root.exists():
        return 0
    cutoff = time.time() - max_age_hours * 3600
    removed = 0
    for folder in root.iterdir():
        if folder.is_dir() and folder.stat().st_mtime < cutoff:
            active = svc.db.one("SELECT 1 AS x FROM projects WHERE id = ? AND status IN "
                                "('queued','acquiring','preparing_audio','transcribing','planning','rendering')",
                                (folder.name,))
            if not active:
                shutil.rmtree(folder, ignore_errors=True)
                removed += 1
    return removed


def run_once(svc: Services) -> dict:
    from .backup import create_backup

    result = {"uploads_removed": cleanup_expired_uploads(svc), "work_removed": cleanup_stale_work(svc)}
    return result


class Maintenance:
    def __init__(self, svc: Services):
        self.svc = svc
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_backup = 0.0

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="clipper-maintenance", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        from .backup import create_backup

        while not self._stop.is_set():
            try:
                result = run_once(self.svc)
                log.info("Maintenance: %s", result)
                hours = self.svc.settings.backup_every_hours
                if hours > 0 and time.time() - self._last_backup >= hours * 3600:
                    path = create_backup(self.svc.settings)
                    self._last_backup = time.time()
                    log.info("Scheduled backup: %s", path)
            except Exception as exc:
                log.exception("Maintenance failed: %s", exc)
            self._stop.wait(INTERVAL_SECONDS)
