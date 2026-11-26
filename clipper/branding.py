"""Brand logo rules and clip-mix limits. One logo per user. Where it sits is stored as
percentages of the frame: x and y are 0 to 100, where 0 is the left or top edge and 100 is
the right or bottom edge. The logo's size is a percentage of the frame width."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

LOGO_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp")
LOGO_MAX_BYTES = 5 * 1024 * 1024
MOMENTS_MAX = 6          # moments joined into one mixed clip
MOMENTS_MIN_SECONDS = 4.0  # shortest moment allowed inside a mixed clip

# Position presets, as (x, y) percentages. Each sits a little in from the edge.
SPOTS = {
    "tl": (3, 3), "tc": (50, 3), "tr": (97, 3),
    "ml": (3, 50), "mc": (50, 50), "mr": (97, 50),
    "bl": (3, 97), "bc": (50, 97), "br": (97, 97),
}

LOGO_DEFAULTS = {"enabled": False, "opacity": 80, "size": 15, "x": 97, "y": 3}


def logo_key(user_id: str) -> str:
    return f"logos/{user_id}/logo.png"


def clean_logo(raw: Optional[dict]) -> dict:
    """Clean logo options from a request or a saved default. Out-of-range numbers are
    brought back into range, so a stored value can never break rendering."""
    raw = raw or {}

    def number(key: str, lo: float, hi: float) -> float:
        try:
            value = float(raw.get(key, LOGO_DEFAULTS[key]))
        except (TypeError, ValueError):
            value = float(LOGO_DEFAULTS[key])
        return round(min(hi, max(lo, value)), 1)

    return {
        "enabled": bool(raw.get("enabled", False)),
        "opacity": number("opacity", 0, 100),
        "size": number("size", 5, 50),
        "x": number("x", 0, 100),
        "y": number("y", 0, 100),
    }


def check_logo_file(name: str, head: bytes) -> str:
    """Return the file extension if the upload looks like a PNG, JPG or WebP image.
    Raises ValueError with a message for the user otherwise."""
    ext = Path(name or "").suffix.lower()
    if ext not in LOGO_EXTENSIONS:
        raise ValueError("Upload a PNG, JPG or WebP image. A PNG with a transparent background works best.")
    is_png = head.startswith(b"\x89PNG")
    is_jpg = head.startswith(b"\xff\xd8\xff")
    is_webp = head[:4] == b"RIFF" and head[8:12] == b"WEBP"
    if not (is_png or is_jpg or is_webp):
        raise ValueError("This file is not a real PNG, JPG or WebP image")
    return ext
