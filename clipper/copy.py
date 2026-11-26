"""Recommended title and description for each clip. The language model writes them when one is
set up. Otherwise they come from the clip's own words. Every version is kept, so asking for a
new one never removes an earlier one."""
from __future__ import annotations

import logging
from typing import Callable, Optional

from .llm import InvalidOutput, LLMProvider, run_with_fallback

log = logging.getLogger("clipper.copy")

MAX_TRANSCRIPT_CHARS = 1500
TITLE_MAX = 70
DESCRIPTION_MAX = 220
SYSTEM_PROMPT = """You write a title and a short description for a short video clip.
Reply with one JSON object and nothing else:
{"title": "<under 70 characters>", "description": "<one or two short sentences>"}
Use only what the transcript says. Do not invent facts."""


def clip_transcript(segments: list[dict], parts: list[tuple[float, float]]) -> str:
    """The words spoken inside the clip's moments, in order, capped in length."""
    words: list[str] = []
    for start, end in parts:
        for seg in segments:
            if seg["end"] <= start or seg["start"] >= end:
                continue
            text = (seg.get("text") or "").strip()
            if text:
                words.append(text)
    return " ".join(" ".join(words).split())[:MAX_TRANSCRIPT_CHARS]


def _shorten(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rsplit(" ", 1)[0] + "…"


def _sentences(text: str) -> list[str]:
    out, current = [], ""
    for ch in text:
        current += ch
        if ch in ".!?":
            if current.strip():
                out.append(current.strip())
            current = ""
    if current.strip():
        out.append(current.strip())
    return out


def heuristic_copy(text: str, variant: int = 0) -> dict:
    """No model: take sentences from the clip itself. Each new version starts from a different
    sentence, so asking again gives a different suggestion."""
    sentences = _sentences(text)
    if not sentences:
        return {"title": "Clip", "description": "", "source": "heuristic"}
    pick = variant % len(sentences)
    title = _shorten(sentences[pick], TITLE_MAX)
    description = _shorten(" ".join(sentences[pick:pick + 2]), DESCRIPTION_MAX)
    return {"title": title, "description": description, "source": "heuristic"}


def _validate(obj: dict) -> dict:
    title = str(obj.get("title", "")).strip()[:120]
    description = str(obj.get("description", "")).strip()[:400]
    if not title:
        raise InvalidOutput("the title is empty")
    return {"title": title, "description": description}


def write_copy(
    text: str,
    providers: list[LLMProvider],
    variant: int = 0,
    on_usage: Optional[Callable] = None,
    on_failure: Optional[Callable] = None,
) -> dict:
    """A new title and description. Falls back to the clip's own words when no model answers."""
    if not text.strip():
        return {"title": "Clip", "description": "", "source": "heuristic"}
    if providers:
        try:
            result, name = run_with_fallback(
                providers, SYSTEM_PROMPT, "Transcript of the clip:\n" + text,
                validate=_validate, on_usage=on_usage, on_failure=on_failure, max_tokens=400,
            )
            return {**result, "source": name}
        except Exception as exc:  # every provider failed: use the clip's own words
            log.warning("Title and description fell back to the transcript: %s", exc)
    return heuristic_copy(text, variant)


def load_copies(raw: Optional[str]) -> dict:
    """The stored history: {"versions": [...], "active": index}. Empty when nothing is stored."""
    from .db import loads

    data = loads(raw, None) or {}
    versions = [v for v in data.get("versions", []) if isinstance(v, dict)]
    active = data.get("active", len(versions) - 1)
    if not versions:
        return {"versions": [], "active": 0}
    if not isinstance(active, int) or not 0 <= active < len(versions):
        active = len(versions) - 1
    return {"versions": versions, "active": active}


def current_copy(raw: Optional[str]) -> dict:
    data = load_copies(raw)
    if not data["versions"]:
        return {"title": "", "description": "", "source": None}
    return data["versions"][data["active"]]
