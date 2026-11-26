"""Admin management of the effect library from the website: create and delete themes, add and remove
sound or B-roll files. Each change writes the theme's manifest.json, so the files on disk and the
library always agree. Uploads are checked to be real media before they are kept."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Callable, Optional

from .config import Settings

SOUND_EXT = {".wav", ".mp3", ".ogg", ".m4a", ".flac"}
VIDEO_EXT = {".mp4", ".webm", ".mov"}
MAX_UPLOAD_BYTES = 80 * 1024 * 1024
MAX_TAGS = 12
FOLDER_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")


class LibraryError(ValueError):
    """A request the library cannot accept. The message is shown to the admin."""


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").strip().lower()).strip("-")
    if not slug:
        raise LibraryError("Use letters or numbers in the name")
    return slug[:40]


def _theme_dirs(settings: Settings) -> list[Path]:
    if not settings.effects_dir.exists():
        return []
    return sorted(p for p in settings.effects_dir.iterdir() if p.is_dir() and (p / "manifest.json").is_file())


def _folder(settings: Settings, folder_name: str) -> Path:
    if not FOLDER_RE.match(folder_name or ""):
        raise LibraryError("That theme does not exist")
    folder = settings.effects_dir / folder_name
    if not (folder / "manifest.json").is_file():
        raise LibraryError("That theme does not exist")
    return folder


def read_manifest(folder: Path) -> dict:
    data = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    data.setdefault("display_name", folder.name)
    data.setdefault("description", "")
    data.setdefault("effects", [])
    return data


def write_manifest(folder: Path, manifest: dict) -> None:
    text = json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
    tmp = folder / "manifest.json.tmp"
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(folder / "manifest.json")


def create_theme(settings: Settings, name: str, description: str) -> dict:
    name = (name or "").strip()[:60]
    if len(name) < 2:
        raise LibraryError("Give the theme a name (at least 2 characters)")
    folder_name = slugify(name)
    for other in _theme_dirs(settings):
        if other.name == folder_name or read_manifest(other)["display_name"].lower() == name.lower():
            raise LibraryError("A theme with this name already exists")
    folder = settings.effects_dir / folder_name
    (folder / "sfx").mkdir(parents=True, exist_ok=True)
    (folder / "broll").mkdir(parents=True, exist_ok=True)
    write_manifest(folder, {"display_name": name, "description": (description or "").strip()[:300], "effects": []})
    return {"folder": folder_name, "name": name}


def delete_theme(settings: Settings, folder_name: str) -> None:
    import shutil

    folder = _folder(settings, folder_name)
    shutil.rmtree(folder)


def _safe_rel(rel: str) -> str:
    rel = (rel or "").replace("\\", "/")
    parts = rel.split("/")
    if len(parts) != 2 or parts[0] not in ("sfx", "broll") or parts[1] in ("", ".", "..") or ".." in parts:
        raise LibraryError("That file is not part of this theme")
    return rel


def add_effect(
    settings: Settings,
    folder_name: str,
    filename: str,
    data: bytes,
    *,
    kind: str,
    name: str,
    tags: list[str],
    license_text: str,
    source: str,
    speech: bool,
    probe: Callable[[Path], float],
) -> dict:
    folder = _folder(settings, folder_name)
    if kind not in ("sound", "broll"):
        raise LibraryError("Choose Sound or B-roll")
    if not data:
        raise LibraryError("The file is empty")
    if len(data) > MAX_UPLOAD_BYTES:
        raise LibraryError("The file is larger than 80 MB")
    ext = Path(filename or "").suffix.lower()
    allowed = SOUND_EXT if kind == "sound" else VIDEO_EXT
    if ext not in allowed:
        kinds = ", ".join(sorted(allowed))
        raise LibraryError(f"For this kind use one of: {kinds}")

    base = slugify(name or Path(filename).stem)
    subdir = "sfx" if kind == "sound" else "broll"
    rel = f"{subdir}/{base}{ext}"
    n = 2
    while (folder / rel).exists():
        rel = f"{subdir}/{base}-{n}{ext}"
        n += 1
    target = folder / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    try:
        probe(target)  # raises if FFmpeg cannot read the file
    except Exception as exc:
        target.unlink(missing_ok=True)
        raise LibraryError("This file could not be read as audio or video. Try a different file.") from exc

    clean_tags = []
    for tag in tags:
        t = re.sub(r"\s+", " ", tag.strip().lower())[:30]
        if t and t not in clean_tags:
            clean_tags.append(t)
    entry = {
        "file": rel,
        "kind": kind,
        "tags": clean_tags[:MAX_TAGS],
        "license": (license_text or "").strip()[:200] or "Not stated: check before publishing",
        "source": (source or "").strip()[:300],
    }
    if speech:
        entry["speech"] = True
    manifest = read_manifest(folder)
    manifest["effects"].append(entry)
    write_manifest(folder, manifest)
    return entry


def remove_effect(settings: Settings, folder_name: str, rel: str) -> None:
    folder = _folder(settings, folder_name)
    rel = _safe_rel(rel)
    manifest = read_manifest(folder)
    before = len(manifest["effects"])
    manifest["effects"] = [e for e in manifest["effects"] if e.get("file") != rel]
    if len(manifest["effects"]) == before:
        raise LibraryError("That effect is not in this theme")
    write_manifest(folder, manifest)
    (folder / rel).unlink(missing_ok=True)


def find_ffmpeg_probe(svc) -> Callable[[Path], float]:
    if svc.ffmpeg is None:
        raise LibraryError("FFmpeg is not available on this server")
    return lambda path: svc.ffmpeg.probe(path)["duration"]


def theme_folder_names(settings: Settings) -> dict[str, str]:
    """Map of display name to folder name, for every theme."""
    out: dict[str, str] = {}
    for folder in _theme_dirs(settings):
        out[read_manifest(folder)["display_name"]] = folder.name
    return out


def optional_folder(settings: Settings, folder_name: Optional[str]) -> Optional[Path]:
    return _folder(settings, folder_name) if folder_name else None
