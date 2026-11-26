"""Effect library. Phase 1 stub: no effects are available yet. Replaced in Phase 2."""
from __future__ import annotations

DEFAULT_THEMES = ["Funny", "Gaming", "Inspirational", "Podcast Shorts"]


def list_themes(svc) -> list[str]:
    return list(DEFAULT_THEMES)


def catalog_for_planner(svc, theme: str) -> list[dict]:
    return []


def resolve_placements(svc, theme: str, placements: list[dict], clip_length: float):
    return iter(())
