"""Backups of the database, settings, and effect manifests.

The master key is never included. Keep a copy somewhere safe, separately from the backups:
without it, the saved API keys in the database cannot be decrypted.
"""
from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import subprocess
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from .config import Settings

log = logging.getLogger("clipper.backup")
PREFIX = "clipper-backup-"


def _dump_sqlite(settings: Settings, dst: Path) -> None:
    src_path = settings.sqlite_path
    if src_path is None or not src_path.exists():
        raise RuntimeError("SQLite database not found")
    src = sqlite3.connect(str(src_path))
    dest = sqlite3.connect(str(dst))
    try:
        src.backup(dest)  # consistent copy, safe while the app is running
    finally:
        dest.close()
        src.close()


def _dump_postgres(settings: Settings, dst: Path) -> None:
    pg_dump = shutil.which("pg_dump")
    if pg_dump is None:
        raise RuntimeError("pg_dump was not found on PATH. Install the PostgreSQL client tools.")
    proc = subprocess.run([pg_dump, "--format=custom", "--no-owner", "--file", str(dst), settings.database_url],
                          capture_output=True, text=True, timeout=3600)
    if proc.returncode != 0:
        raise RuntimeError("pg_dump failed: " + proc.stderr.strip()[-400:])


def _safe_settings(settings: Settings) -> dict:
    """Settings that are safe to store next to the backup. Secrets are left out."""
    return {
        "mode": settings.mode, "storage": settings.storage, "s3_bucket": settings.s3_bucket,
        "s3_prefix": settings.s3_prefix, "s3_region": settings.s3_region,
        "whisper_model": settings.whisper_model, "upload_max_mb": settings.upload_max_mb,
        "max_duration_sec": settings.max_duration_sec, "worker_concurrency": settings.worker_concurrency,
        "max_effects_per_clip": settings.max_effects_per_clip,
        "database": "postgresql" if settings.database_url.startswith(("postgres", "postgresql")) else "sqlite",
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def create_backup(settings: Settings) -> Path:
    settings.backups_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    archive = settings.backups_dir / f"{PREFIX}{stamp}.zip"
    with tempfile.TemporaryDirectory(prefix="backup-") as tmp:
        tmp_path = Path(tmp)
        if settings.sqlite_path is not None:
            db_file = tmp_path / "database.sqlite3"
            _dump_sqlite(settings, db_file)
        else:
            db_file = tmp_path / "database.pgdump"
            _dump_postgres(settings, db_file)
        settings_file = tmp_path / "settings.json"
        settings_file.write_text(json.dumps(_safe_settings(settings), indent=2), encoding="utf-8")
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.write(db_file, arcname=db_file.name)
            zf.write(settings_file, arcname="settings.json")
            if settings.effects_dir.exists():
                for manifest in sorted(settings.effects_dir.glob("*/manifest.json")):
                    zf.write(manifest, arcname=f"effects/{manifest.parent.name}/manifest.json")
    _prune(settings)
    log.info("Backup written: %s", archive)
    return archive


def _prune(settings: Settings) -> None:
    backups = sorted(settings.backups_dir.glob(f"{PREFIX}*.zip"))
    for old in backups[: max(0, len(backups) - max(1, settings.backup_keep))]:
        old.unlink(missing_ok=True)


def list_backups(settings: Settings) -> list[dict]:
    if not settings.backups_dir.exists():
        return []
    return [
        {"name": p.name, "size": p.stat().st_size,
         "created": datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")}
        for p in sorted(settings.backups_dir.glob(f"{PREFIX}*.zip"), reverse=True)
    ]
