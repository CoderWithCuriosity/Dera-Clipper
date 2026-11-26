"""Accounts and sign-in: password hashing, short-lived access tokens, rotating refresh tokens,
and rate limits for sign-in attempts and API calls."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Optional

from .config import Settings
from .db import Database, new_id, now_iso

ACCESS_TTL_SECONDS = 15 * 60
REFRESH_TTL_DAYS = 30
MIN_PASSWORD_LENGTH = 10
_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1}


class AuthError(Exception):
    def __init__(self, message: str, status: int = 401):
        super().__init__(message)
        self.status = status


# -- passwords -------------------------------------------------------------------

def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, dklen=32, **_SCRYPT)
    return "scrypt${}${}${}${}${}".format(
        _SCRYPT["n"], _SCRYPT["r"], _SCRYPT["p"],
        base64.b64encode(salt).decode("ascii"), base64.b64encode(digest).decode("ascii"),
    )


def verify_password(password: str, stored: Optional[str]) -> bool:
    if not stored or not stored.startswith("scrypt$"):
        return False
    try:
        _, n, r, p, salt_b64, digest_b64 = stored.split("$")
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        actual = hashlib.scrypt(password.encode("utf-8"), salt=salt, dklen=len(expected),
                                n=int(n), r=int(r), p=int(p))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def check_password_rules(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise AuthError(f"Passwords need at least {MIN_PASSWORD_LENGTH} characters", 400)
    if len(password) > 256:
        raise AuthError("Password is too long", 400)


# -- access tokens (short-lived, signed) -----------------------------------------

def _signing_key(settings: Settings) -> bytes:
    from .security import _master_key_material

    return hashlib.sha256(b"access-token:" + _master_key_material(settings)).digest()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def issue_access_token(settings: Settings, user: dict) -> str:
    payload = {"sub": user["id"], "role": user["role"], "exp": int(time.time()) + ACCESS_TTL_SECONDS,
               "jti": secrets.token_hex(8)}
    body = _b64(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig = _b64(hmac.new(_signing_key(settings), body.encode("ascii"), hashlib.sha256).digest())
    return f"{body}.{sig}"


MEDIA_TTL_SECONDS = 3600


def _sign(settings: Settings, payload: dict) -> str:
    body = _b64(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig = _b64(hmac.new(_signing_key(settings), body.encode("ascii"), hashlib.sha256).digest())
    return f"{body}.{sig}"


def _verify(settings: Settings, token: str) -> Optional[dict]:
    try:
        body, sig = token.split(".", 1)
    except ValueError:
        return None
    expected = _b64(hmac.new(_signing_key(settings), body.encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        payload = json.loads(_unb64(body))
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict) or int(payload.get("exp", 0)) < time.time():
        return None
    return payload


def read_access_token(settings: Settings, token: str) -> Optional[dict]:
    payload = _verify(settings, token)
    if payload is None or payload.get("typ", "access") != "access":
        return None
    return payload


def issue_media_token(settings: Settings, user_id: str, ref: str) -> str:
    """A link for the browser's own video and download requests, which cannot send the sign-in
    header. It only works for the one clip or project named in `ref`, and expires after an hour."""
    return _sign(settings, {"sub": user_id, "ref": ref, "typ": "media",
                            "exp": int(time.time()) + MEDIA_TTL_SECONDS, "jti": secrets.token_hex(6)})


def read_media_token(settings: Settings, token: str, ref: str) -> Optional[dict]:
    payload = _verify(settings, token)
    if payload is None or payload.get("typ") != "media" or payload.get("ref") != ref:
        return None
    return payload


# -- refresh tokens (stored hashed, rotated on every use) --------------------------

def _hash_refresh(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue_refresh_token(db: Database, user_id: str) -> str:
    token = secrets.token_urlsafe(32)
    expires = (datetime.now(timezone.utc) + timedelta(days=REFRESH_TTL_DAYS)).isoformat(timespec="seconds")
    db.run(
        "INSERT INTO refresh_tokens (id, user_id, token_hash, expires_at, revoked, created_at) VALUES (?, ?, ?, ?, 0, ?)",
        (new_id(), user_id, _hash_refresh(token), expires, now_iso()),
    )
    return token


def rotate_refresh_token(db: Database, token: str) -> Optional[str]:
    """Return the user id for a valid token and retire it. Reusing a retired token revokes
    every session for that user, since it probably means the token was stolen."""
    row = db.one("SELECT * FROM refresh_tokens WHERE token_hash = ?", (_hash_refresh(token),))
    if row is None:
        return None
    if row["revoked"]:
        db.run("UPDATE refresh_tokens SET revoked = 1 WHERE user_id = ?", (row["user_id"],))
        return None
    if row["expires_at"] < now_iso():
        return None
    db.run("UPDATE refresh_tokens SET revoked = 1 WHERE id = ?", (row["id"],))
    return row["user_id"]


def revoke_all(db: Database, user_id: str) -> None:
    db.run("UPDATE refresh_tokens SET revoked = 1 WHERE user_id = ?", (user_id,))


# -- rate limiting ----------------------------------------------------------------

class RateLimiter:
    """Sliding window in memory. For several server processes, put the limit in the proxy."""

    def __init__(self):
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str, limit: int, window_seconds: float) -> bool:
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and now - q[0] > window_seconds:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(now)
            return True


def cookie_for_refresh(token: str, secure: bool) -> dict:
    return {
        "key": "clipper_refresh", "value": token, "httponly": True, "samesite": "lax",
        "secure": secure, "path": "/api/auth", "max_age": REFRESH_TTL_DAYS * 24 * 3600,
    }

