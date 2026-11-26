"""Storage adapters. Callers use keys such as "uploads/<project>/source.mp4" and never
know whether the bytes live on local disk or in S3."""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Optional, Protocol

from .config import Settings


class Storage(Protocol):
    def put_file(self, key: str, src: Path) -> None: ...
    def local_path(self, key: str) -> Path: ...
    def exists(self, key: str) -> bool: ...
    def delete(self, key: str) -> None: ...
    def delete_prefix(self, prefix: str) -> None: ...
    def download_url(self, key: str, filename: str) -> Optional[str]: ...
    def list_keys(self, prefix: str) -> list[str]: ...


def _safe_key(key: str) -> str:
    parts = [p for p in key.replace("\\", "/").split("/") if p not in ("", ".", "..")]
    if not parts:
        raise ValueError("Empty storage key")
    return "/".join(parts)


class LocalStorage:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        return self.root.joinpath(*_safe_key(key).split("/"))

    def put_file(self, key: str, src: Path) -> None:
        dst = self._path(key)
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.resolve() != dst.resolve():
            shutil.copyfile(src, dst)

    def local_path(self, key: str) -> Path:
        path = self._path(key)
        if not path.exists():
            raise FileNotFoundError(key)
        return path

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def delete(self, key: str) -> None:
        path = self._path(key)
        if path.exists():
            path.unlink()

    def delete_prefix(self, prefix: str) -> None:
        path = self._path(prefix)
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        elif path.exists():
            path.unlink()

    def download_url(self, key: str, filename: str, attachment: bool = True) -> Optional[str]:
        return None  # served by the API itself

    def list_keys(self, prefix: str) -> list[str]:
        base = self._path(prefix)
        if not base.exists():
            return []
        return sorted(
            p.relative_to(self.root).as_posix() for p in base.rglob("*") if p.is_file()
        )


def build_storage(settings: Settings) -> Storage:
    if settings.storage == "s3":
        from .storage_s3 import S3Storage  # imported lazily: boto3 is optional

        return S3Storage(settings)
    return LocalStorage(settings.data_dir / "objects")
