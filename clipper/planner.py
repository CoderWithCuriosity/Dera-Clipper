"""Planner: chooses the clip moments. The language model reads transcript text only and
returns candidate moments. This module checks and repairs them so every clip is valid,
non-overlapping, and close to the requested length."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Optional

from .db import loads
from .llm import InvalidOutput, LLMProvider, run_with_fallback

log = logging.getLogger("clipper.planner")

HEURISTIC = "heuristic"
SECTION_CHARS = 9000


@dataclass
class Moment:
    start: float
    end: float
    score: float
    reason: str
    effects: list[dict] = field(default_factory=list)  # {effect_id, at, kind}


SYSTEM_PROMPT = """You are a video editor who picks short, engaging clips from a transcript.
You receive text only. Reply with a single JSON object and nothing else, in this exact shape:
{"moments": [{"start": <seconds>, "end": <seconds>, "score": <0..1>, "reason": "<one sentence>",
  "effects": [{"effect_id": "<id from the list>", "at": <seconds>, "kind": "sound" or "broll"}]}]}

Rules:
- Use only times and words that appear in the transcript. Do not invent content.
- Each moment should be a complete thought that starts and ends at a sentence boundary.
- Moments must not overlap each other.
- Respect the requested clip length as closely as possible.
- score is how engaging the moment is for short-form viewers (1 = excellent).
- "effects" is optional. Only use effect ids from the list you are given, and place them
  at a sentence end or a pause. Leave the list empty if nothing fits.
"""


def _overlaps(a: tuple[float, float], b: tuple[float, float]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def chunk_segments(segments: list[dict], max_chars: int = SECTION_CHARS) -> list[list[dict]]:
    """Group consecutive segments into sections small enough for one model request."""
    sections: list[list[dict]] = []
    current: list[dict] = []
    size = 0
    for seg in segments:
        length = len(seg["text"]) + 16
        if current and size + length > max_chars:
            sections.append(current)
            current, size = [], 0
        current.append(seg)
        size += length
    if current:
        sections.append(current)
    return sections


def _format_section(section: list[dict]) -> str:
    return "\n".join(f"[{s['start']:.2f}-{s['end']:.2f}] {s['text']}" for s in section)


def build_user_prompt(theme: str, clip_length: float, count: int, section: list[dict],
                      effects_catalog: list[dict]) -> str:
    lines = [
        f"Theme: {theme}",
        f"Requested clip length: about {clip_length:.0f} seconds (acceptable {clip_length*0.8:.0f}-{clip_length*1.2:.0f}).",
        f"Return up to {count} moments from the transcript section below.",
    ]
    if effects_catalog:
        lines.append("Available effects (id, kind, tags):")
        for e in effects_catalog:
            line = f"- {e['id']} ({e['kind']}; {', '.join(e.get('tags', [])) or 'no tags'})"
            if e.get("says"):
                line += f' says: "{e["says"]}"'
            lines.append(line)
    lines.append("Transcript section (times in seconds):")
    lines.append(_format_section(section))
    return "\n".join(lines)


def validate_moments(obj: dict, duration: float) -> list[dict]:
    moments = obj.get("moments")
    if not isinstance(moments, list):
        raise InvalidOutput("'moments' must be a list")
    out: list[dict] = []
    for raw in moments:
        if not isinstance(raw, dict):
            continue
        try:
            start = float(raw["start"])
            end = float(raw["end"])
            score = float(raw.get("score", 0.5))
        except (KeyError, TypeError, ValueError):
            continue
        if not (0 <= start < end) or start >= duration:
            continue
        out.append({
            "start": start,
            "end": min(end, duration),
            "score": min(1.0, max(0.0, score)),
            "reason": str(raw.get("reason", "")).strip()[:500],
            "effects": raw.get("effects") if isinstance(raw.get("effects"), list) else [],
        })
    if moments and not out:
        raise InvalidOutput("None of the moments had valid times")
    return out


def _snap(value: float, points: list[float], tolerance: float = 2.0) -> float:
    if not points:
        return value
    nearest = min(points, key=lambda p: abs(p - value))
    return nearest if abs(nearest - value) <= tolerance else value


def normalise_moment(m: dict, clip_length: float, duration: float,
                     starts: list[float], ends: list[float]) -> Optional[dict]:
    """Snap to sentence boundaries and force the length into the allowed range."""
    s = _snap(max(0.0, m["start"]), starts)
    e = _snap(min(duration, m["end"]), ends)
    if e - s > clip_length * 1.25:
        e = s + clip_length
    if e - s < clip_length * 0.6:
        e = s + clip_length
    if e > duration:
        e = duration
        s = max(0.0, e - clip_length)
    if e - s < 1.0:
        return None
    out = dict(m)
    out["start"], out["end"] = round(s, 3), round(e, 3)
    return out


def heuristic_moments(segments: list[dict], clip_length: float, duration: float, count: int) -> list[dict]:
    """No model available: pick the windows with the densest speech."""
    if not segments:
        return []
    step = max(1.0, clip_length / 2)
    candidates: list[tuple[float, float, float]] = []  # (density, start, end)
    t = 0.0
    while t + clip_length <= duration + 1e-6:
        end = t + clip_length
        words = 0
        for seg in segments:
            if seg["end"] <= t or seg["start"] >= end:
                continue
            words += len(seg["text"].split())
        candidates.append((words / clip_length, t, end))
        t += step
    if not candidates:
        return []
    best = max(c[0] for c in candidates) or 1.0
    candidates.sort(reverse=True)
    chosen: list[tuple[float, float]] = []
    picks: list[dict] = []
    for density, s, e in candidates:
        if any(_overlaps((s, e), c) for c in chosen):
            continue
        chosen.append((s, e))
        picks.append({
            "start": round(s, 3), "end": round(e, 3),
            "score": round(0.3 + 0.4 * density / best, 3),
            "reason": "Automatic pick: the speech density is high in this section (no language model was used).",
            "effects": [],
        })
        if len(picks) >= count:
            break
    return picks


def group_highlights(moments: list[Moment], per_clip: int, clip_count: int) -> list[list[Moment]]:
    """Turn ranked moments into mixed clips of per_clip moments each.

    The best moments are dealt out in turns, so every clip gets a mix of strong and weaker
    moments instead of one clip holding all the best ones. Inside each clip the moments are
    put back in video order, so the joined clip plays in the order things happened. Fewer
    than clip_count clips are made when there are not enough moments for all of them."""
    if not moments:
        return []
    ranked = sorted(moments, key=lambda m: m.score, reverse=True)
    groups = max(1, min(clip_count, len(ranked) // max(1, per_clip)))
    pool = ranked[: groups * per_clip] if len(ranked) >= groups * per_clip else ranked
    buckets: list[list[Moment]] = [[] for _ in range(groups)]
    for i, moment in enumerate(pool):
        buckets[i % groups].append(moment)
    return [sorted(b, key=lambda m: m.start) for b in buckets if b]


def clip_parts(start: float, end: float, segments_json: Optional[str]) -> list[tuple[float, float]]:
    """The moments a clip is made of. A normal clip has one moment: its start and end."""
    parts = loads(segments_json, None)
    if not parts:
        return [(float(start), float(end))]
    return [(float(a), float(b)) for a, b in parts]


def plan_moments(
    segments: list[dict],
    duration: float,
    theme: str,
    clip_length: float,
    clip_count: int,
    providers: list[LLMProvider],
    effects_catalog: Optional[list[dict]] = None,
    on_usage: Optional[Callable] = None,
    on_failure: Optional[Callable] = None,
) -> tuple[list[Moment], str]:
    """Return (moments, provider name). The provider name is "heuristic" when no model
    answered."""
    effects_catalog = effects_catalog or []
    starts = [s["start"] for s in segments]
    ends = [s["end"] for s in segments]
    per_section = max(2, clip_count)
    candidates: list[dict] = []
    used_provider = HEURISTIC

    if providers:
        for section in chunk_segments(segments):
            user = build_user_prompt(theme, clip_length, per_section, section, effects_catalog)
            try:
                found, name = run_with_fallback(
                    providers, SYSTEM_PROMPT, user,
                    validate=lambda obj: validate_moments(obj, duration),
                    on_usage=on_usage, on_failure=on_failure, max_tokens=2000,
                )
            except Exception as exc:  # every provider failed for this section
                log.warning("Planning failed for one section: %s", exc)
                continue
            used_provider = name
            candidates.extend(found)

    normalised = []
    for m in candidates:
        fixed = normalise_moment(m, clip_length, duration, starts, ends)
        if fixed:
            normalised.append(fixed)
    normalised.sort(key=lambda m: m["score"], reverse=True)

    chosen: list[dict] = []
    taken: list[tuple[float, float]] = []
    for m in normalised:
        span = (m["start"], m["end"])
        if any(_overlaps(span, t) for t in taken):
            continue
        taken.append(span)
        chosen.append(m)
        if len(chosen) >= clip_count:
            break

    if len(chosen) < clip_count:
        # Not enough model moments. Fill the rest with heuristic picks that do not overlap.
        for extra in heuristic_moments(segments, clip_length, duration, clip_count * 2):
            span = (extra["start"], extra["end"])
            if any(_overlaps(span, t) for t in taken):
                continue
            taken.append(span)
            chosen.append(extra)
            if len(chosen) >= clip_count:
                break
        if chosen and used_provider != HEURISTIC and any(c["reason"].startswith("Automatic pick") for c in chosen):
            used_provider = f"{used_provider}+heuristic"

    if not chosen:
        used_provider = HEURISTIC

    moments = [
        Moment(start=c["start"], end=c["end"], score=c["score"], reason=c["reason"],
               effects=c.get("effects") or [])
        for c in chosen
    ]
    return moments, used_provider
