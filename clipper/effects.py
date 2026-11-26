"""Effect library.

Layout (managed by the admin, which is the user in solo mode):

    effects/
      funny/
        manifest.json     display name, description, and one entry per file
        sfx/              sound effects
        broll/            short reaction or meme clips

Adding files and clicking "Rescan library" adds them. No code change is needed.
"""
from __future__ import annotations

import hashlib
import json
import logging
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .db import dumps, loads, now_iso

log = logging.getLogger("clipper.effects")

DEFAULT_THEMES = ["Funny", "Gaming", "Inspirational", "Podcast Shorts"]
KINDS = ("sound", "broll")
MIN_GAP = 2.0            # seconds between two effect placements in one clip
BOUNDARY_TOLERANCE = 1.0  # a placement snaps to a sentence end or pause within this window
PAUSE_SECONDS = 0.3       # a gap between words at least this long counts as a pause


@dataclass
class ScanSummary:
    themes: int = 0
    effects: int = 0
    transcribed: int = 0
    warnings: list[str] = None  # type: ignore[assignment]

    def to_dict(self) -> dict:
        return {"themes": self.themes, "effects": self.effects, "transcribed": self.transcribed,
                "warnings": self.warnings or []}


# -- manifests ---------------------------------------------------------------

def _theme_folders(settings) -> list[Path]:
    if not settings.effects_dir.exists():
        return []
    return sorted(p for p in settings.effects_dir.iterdir() if p.is_dir() and (p / "manifest.json").is_file())


def _load_manifest(folder: Path) -> Optional[dict]:
    try:
        data = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("Skipping %s: %s", folder, exc)
        return None
    if not isinstance(data, dict):
        return None
    data.setdefault("display_name", folder.name.replace("-", " ").title())
    data.setdefault("effects", [])
    return data


def list_themes(svc) -> list[str]:
    names = []
    for folder in _theme_folders(svc.settings):
        manifest = _load_manifest(folder)
        if manifest:
            names.append(str(manifest["display_name"]))
    return names or list(DEFAULT_THEMES)


def _folder_for_theme(svc, theme: str) -> Optional[Path]:
    for folder in _theme_folders(svc.settings):
        manifest = _load_manifest(folder)
        if manifest and manifest["display_name"] == theme:
            return folder
    return None


# -- scanning ----------------------------------------------------------------

def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _transcribe_for_cache(svc, path: Path) -> Optional[dict]:
    """Transcribe an effect file that contains speech, once. The cache is keyed by content."""
    from .transcription import build_local_engine

    try:
        engine = build_local_engine(svc.settings)
        engine.check()
        transcript = engine.transcribe(path)
    except Exception as exc:
        log.warning("Could not transcribe effect %s: %s", path.name, exc)
        return None
    return {"text": " ".join(s["text"] for s in transcript.segments).strip(),
            "segments": transcript.segments}


def rescan(svc) -> dict:
    summary = ScanSummary(warnings=[])
    seen: set[str] = set()
    ff = svc.ffmpeg

    for folder in _theme_folders(svc.settings):
        manifest = _load_manifest(folder)
        if not manifest:
            continue
        summary.themes += 1
        theme = str(manifest["display_name"])
        for entry in manifest.get("effects", []):
            if not isinstance(entry, dict) or not entry.get("file"):
                summary.warnings.append(f"{theme}: an entry has no file name")
                continue
            rel = str(entry["file"]).replace("\\", "/")
            if rel.startswith("/") or ".." in rel.split("/"):
                summary.warnings.append(f"{theme}: '{rel}' must be a path inside the theme folder")
                continue
            kind = entry.get("kind", "sound")
            if kind not in KINDS:
                summary.warnings.append(f"{theme}: '{rel}' has an unknown kind '{kind}'")
                continue
            path = folder / rel
            if not path.is_file():
                summary.warnings.append(f"{theme}: '{rel}' is listed in the manifest but missing on disk")
                continue

            digest = _sha256(path)
            effect_id = f"{folder.name}/{rel}"
            storage_key = f"effects/{folder.name}/{rel}"
            duration = None
            if ff is not None:
                try:
                    duration = ff.probe(path)["duration"]
                except Exception as exc:
                    summary.warnings.append(f"{theme}: could not read '{rel}': {exc}")

            existing = svc.db.one("SELECT * FROM effects WHERE id = ?", (effect_id,))
            transcript_json = None
            if entry.get("speech"):
                if existing and existing["content_hash"] == digest and existing["transcript_json"]:
                    transcript_json = existing["transcript_json"]  # unchanged file, keep the cache
                else:
                    donor = svc.db.one(
                        "SELECT transcript_json FROM effects WHERE content_hash = ? AND transcript_json IS NOT NULL LIMIT 1",
                        (digest,),
                    )
                    if donor:  # renamed or moved file: reuse by content, no new transcription
                        transcript_json = donor["transcript_json"]
                    else:
                        result = _transcribe_for_cache(svc, path)
                        if result is not None:
                            transcript_json = dumps(result)
                            summary.transcribed += 1
                        else:
                            summary.warnings.append(f"{theme}: '{rel}' has speech but could not be transcribed")

            tags = entry.get("tags") or []
            license_text = " — ".join(x for x in [str(entry.get("license", "")), str(entry.get("source", ""))] if x)
            if svc.settings.storage == "s3":
                if not svc.storage.exists(storage_key) or existing is None or existing["content_hash"] != digest:
                    svc.storage.put_file(storage_key, path)

            svc.db.run(
                "INSERT INTO effects (id, theme, kind, file_name, storage_key, duration, content_hash, "
                "transcript_json, tags_json, license, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (id) DO UPDATE SET theme = excluded.theme, kind = excluded.kind, "
                "duration = excluded.duration, content_hash = excluded.content_hash, "
                "transcript_json = excluded.transcript_json, tags_json = excluded.tags_json, "
                "license = excluded.license",
                (effect_id, theme, kind, rel, storage_key, duration, digest, transcript_json,
                 dumps(tags), license_text or None, now_iso()),
            )
            seen.add(effect_id)

    # Remove effects whose files were deleted or removed from a manifest.
    for row in svc.db.all("SELECT id FROM effects"):
        if row["id"] not in seen:
            svc.db.run("DELETE FROM effects WHERE id = ?", (row["id"],))
    summary.effects = len(seen)
    return summary.to_dict()


# -- planner catalog ------------------------------------------------------------

def _rows(svc, theme: str) -> list[dict]:
    return svc.db.all("SELECT * FROM effects WHERE theme = ? ORDER BY id", (theme,))


def _name_of(row: dict) -> str:
    return Path(row["file_name"]).stem.replace("_", " ").replace("-", " ")


def catalog_for_planner(svc, theme: str) -> list[dict]:
    out = []
    for row in _rows(svc, theme):
        transcript = loads(row["transcript_json"], None)
        item = {
            "id": row["id"],
            "kind": row["kind"],
            "name": _name_of(row),
            "tags": loads(row["tags_json"], []),
        }
        if transcript and transcript.get("text"):
            item["says"] = transcript["text"][:160]
        out.append(item)
    return out


# -- placements -------------------------------------------------------------------

def sentence_boundaries(transcript) -> list[float]:
    """Places where a sound or B-roll can start naturally: sentence ends and pauses."""
    points = [float(s["end"]) for s in transcript.segments]
    words = transcript.words or []
    for a, b in zip(words, words[1:]):
        if float(b["start"]) - float(a["end"]) >= PAUSE_SECONDS:
            points.append(float(a["end"]))
    return sorted({round(p, 3) for p in points})


def _snap(value: float, points: list[float]) -> Optional[float]:
    if not points:
        return None
    nearest = min(points, key=lambda p: abs(p - value))
    return nearest if abs(nearest - value) <= BOUNDARY_TOLERANCE else None


def _valid_time(t: float, moment) -> bool:
    return moment.start + 0.2 <= t <= moment.end - 0.5


def _from_model(proposals: list, moment, catalog: dict, boundaries: list[float], max_n: int) -> list[dict]:
    out: list[dict] = []
    for p in proposals:
        if not isinstance(p, dict):
            continue
        row = catalog.get(str(p.get("effect_id", "")))
        if row is None:
            continue  # the model named an effect that is not in the library
        try:
            at = float(p.get("at"))
        except (TypeError, ValueError):
            continue
        if not _valid_time(at, moment):
            continue
        snapped = _snap(at, boundaries)
        if snapped is None or not _valid_time(snapped, moment):
            continue
        if any(abs(snapped - o["_abs"]) < MIN_GAP for o in out):
            continue
        out.append({"_abs": snapped, "effect_id": row["id"], "kind": row["kind"],
                    "duration": float(row["duration"] or 2.0)})
        if len(out) >= max_n:
            break
    return out


def _fallback(moment, catalog: dict, boundaries: list[float], max_n: int, seed: str) -> list[dict]:
    """No model answered: random effects at sentence ends, capped per clip."""
    pool = list(catalog.values())
    spots = [b for b in boundaries if _valid_time(b, moment)]
    if not pool or not spots:
        return []
    rng = random.Random(f"{seed}:{moment.start:.2f}")
    rng.shuffle(spots)
    chosen: list[float] = []
    for spot in spots:
        if all(abs(spot - c) >= MIN_GAP for c in chosen):
            chosen.append(spot)
        if len(chosen) >= max_n:
            break
    out = []
    for at in sorted(chosen):
        row = rng.choice(pool)
        out.append({"_abs": at, "effect_id": row["id"], "kind": row["kind"],
                    "duration": float(row["duration"] or 2.0)})
    return out


def finalize_placements(svc, theme: str, moments: list, transcript, language_model_answered: bool, seed: str) -> None:
    """Turn each moment's proposals into checked placements, stored relative to the clip start."""
    catalog = {row["id"]: row for row in _rows(svc, theme)}
    max_n = svc.settings.max_effects_per_clip
    boundaries = sentence_boundaries(transcript)
    for m in moments:
        if not catalog:
            m.effects = []
            continue
        if language_model_answered:
            picks = _from_model(m.effects or [], m, catalog, boundaries, max_n)
        else:
            picks = _fallback(m, catalog, boundaries, max_n, seed)
        m.effects = [
            {"effect_id": p["effect_id"], "kind": p["kind"], "at": round(p["_abs"] - m.start, 3),
             "duration": p["duration"]}
            for p in sorted(picks, key=lambda x: x["_abs"])
        ]


def resolve_placements(svc, theme: str, placements: list[dict], clip_length: float):
    """Yield (placement, local file path) for every placement that still exists."""
    settings = svc.settings
    for p in placements:
        row = svc.db.one("SELECT * FROM effects WHERE id = ?", (p.get("effect_id"),))
        if row is None:
            continue
        local = settings.effects_dir.joinpath(*row["storage_key"].split("/")[1:])
        if not local.is_file():
            try:
                local = svc.storage.local_path(row["storage_key"])
            except FileNotFoundError:
                log.warning("Effect file missing: %s", row["storage_key"])
                continue
        yield {**p, "kind": row["kind"], "duration": row["duration"] or p.get("duration", 2.0)}, local
