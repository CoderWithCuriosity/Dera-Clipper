"""Configuration.

Every setting is read from an environment variable named CLIPPER_<NAME>, optionally
loaded from a .env file in the project root. The deployment mode (local, vps, aws)
selects defaults; any individual variable can still override them.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
MODES = ("local", "vps", "aws")


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_dotenv(ROOT / ".env")


def _get(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.environ.get(f"CLIPPER_{name}")
    return value if value not in (None, "") else default


def _bool(name: str, default: bool) -> bool:
    value = _get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    value = _get(name)
    return int(value) if value is not None else default


@dataclass(frozen=True)
class Settings:
    mode: str
    data_dir: Path
    database_url: str
    storage: str                 # "local" or "s3"
    s3_bucket: Optional[str]
    s3_prefix: str
    s3_region: Optional[str]
    s3_endpoint_url: Optional[str]
    effects_dir: Path
    models_dir: Path
    ffmpeg_path: Optional[str]
    whisper_model: str
    whisper_device: str
    default_transcription_engine: str
    upload_max_mb: int
    max_duration_sec: int
    upload_retention_days: int
    worker_concurrency: int
    run_worker: bool
    require_auth: bool
    host: str
    port: int
    max_effects_per_clip: int
    max_projects_per_user_per_day: int
    api_requests_per_minute: int
    job_max_attempts: int

    @property
    def sqlite_path(self) -> Optional[Path]:
        prefix = "sqlite:///"
        if self.database_url.startswith(prefix):
            return Path(self.database_url[len(prefix):])
        return None

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def work_dir(self) -> Path:
        return self.data_dir / "work"

    @property
    def clips_dir(self) -> Path:
        return self.data_dir / "clips"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def key_file(self) -> Path:
        return self.data_dir / "master.key"


def load_settings() -> Settings:
    mode = (_get("MODE", "local") or "local").lower()
    if mode not in MODES:
        raise ValueError(f"CLIPPER_MODE must be one of {MODES}, got {mode!r}")

    data_dir = Path(_get("DATA_DIR", str(ROOT / "data"))).resolve()
    db_default = f"sqlite:///{(data_dir / 'clipper.db').as_posix()}"

    # Mode defaults. Each one can be overridden by its own variable.
    defaults = {
        "local": dict(storage="local", auth=False, engine="local", concurrency=1, whisper="small"),
        "vps": dict(storage="local", auth=True, engine="local", concurrency=1, whisper="base"),
        "aws": dict(storage="s3", auth=True, engine="groq", concurrency=2, whisper="base"),
    }[mode]

    storage = (_get("STORAGE", defaults["storage"]) or "local").lower()
    if storage not in ("local", "s3"):
        raise ValueError("CLIPPER_STORAGE must be 'local' or 's3'")

    return Settings(
        mode=mode,
        data_dir=data_dir,
        database_url=_get("DATABASE_URL", db_default),
        storage=storage,
        s3_bucket=_get("S3_BUCKET"),
        s3_prefix=(_get("S3_PREFIX", "clipper") or "clipper").strip("/"),
        s3_region=_get("S3_REGION"),
        s3_endpoint_url=_get("S3_ENDPOINT_URL"),
        effects_dir=Path(_get("EFFECTS_DIR", str(ROOT / "effects"))).resolve(),
        models_dir=Path(_get("MODELS_DIR", str(data_dir / "models"))).resolve(),
        ffmpeg_path=_get("FFMPEG_PATH"),
        whisper_model=_get("WHISPER_MODEL", defaults["whisper"]),
        whisper_device=_get("WHISPER_DEVICE", "auto"),
        default_transcription_engine=_get("TRANSCRIPTION_ENGINE", defaults["engine"]),
        upload_max_mb=_int("UPLOAD_MAX_MB", 2048),
        max_duration_sec=_int("MAX_DURATION_SEC", 4 * 3600),
        upload_retention_days=_int("UPLOAD_RETENTION_DAYS", 7),
        worker_concurrency=_int("WORKER_CONCURRENCY", defaults["concurrency"]),
        run_worker=_bool("RUN_WORKER", True),
        require_auth=_bool("REQUIRE_AUTH", defaults["auth"]),
        host=_get("HOST", "127.0.0.1" if mode == "local" else "0.0.0.0"),
        port=_int("PORT", 8000),
        max_effects_per_clip=_int("MAX_EFFECTS_PER_CLIP", 4),
        max_projects_per_user_per_day=_int("MAX_PROJECTS_PER_USER_PER_DAY", 50),
        api_requests_per_minute=_int("API_REQUESTS_PER_MINUTE", 300),
        job_max_attempts=_int("JOB_MAX_ATTEMPTS", 3),
    )
