"""Security helpers: key encryption, link validation, upload checks."""
from __future__ import annotations

import base64
import hashlib
import ipaddress
import os
import socket
from pathlib import Path
from urllib.parse import urlparse

from cryptography.fernet import Fernet, InvalidToken

from .config import Settings

ALLOWED_EXTENSIONS = {".mp4", ".mov", ".webm", ".mkv"}


class SecurityError(ValueError):
    """Raised when user input is rejected for security reasons."""


# -- master key and API key encryption ---------------------------------------

def _master_key_material(settings: Settings) -> bytes:
    """The master key lives outside the database: an env var, or a key file with
    restricted permissions created on first run in the data directory."""
    env_key = os.environ.get("CLIPPER_MASTER_KEY")
    if env_key:
        return env_key.encode("utf-8")
    path: Path = settings.key_file
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(base64.urlsafe_b64encode(os.urandom(32)).decode("ascii"), encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    return path.read_text(encoding="utf-8").strip().encode("utf-8")


def _fernet(settings: Settings) -> Fernet:
    digest = hashlib.sha256(_master_key_material(settings)).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_secret(settings: Settings, plain: str) -> str:
    return _fernet(settings).encrypt(plain.encode("utf-8")).decode("ascii")


def decrypt_secret(settings: Settings, token: str) -> str:
    try:
        return _fernet(settings).decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise SecurityError("Stored API key cannot be decrypted; the master key changed") from exc


def mask_secret(plain: str | None) -> str:
    """Only the last four characters are ever shown in the interface."""
    if not plain:
        return ""
    return "••••" + plain[-4:] if len(plain) > 4 else "••••"


# -- links ---------------------------------------------------------------------

def _is_public_ip(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    return not (
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
        or ip.is_reserved or ip.is_unspecified
    )


def validate_link(url: str) -> str:
    """Accept only http(s) URLs whose host resolves exclusively to public addresses.
    This blocks requests to the server's own network (SSRF)."""
    url = (url or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise SecurityError("Only http and https links are allowed")
    host = parsed.hostname
    if not host:
        raise SecurityError("The link has no host name")
    if host.lower() in ("localhost",) or host.lower().endswith(".local"):
        raise SecurityError("Links to local hosts are not allowed")
    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror as exc:
        raise SecurityError(f"Could not resolve host {host}") from exc
    for info in infos:
        address = info[4][0]
        if not _is_public_ip(address):
            raise SecurityError("Links that point to private or internal addresses are blocked")
    return url


# -- uploads -------------------------------------------------------------------

def check_upload_header(path: Path, extension: str) -> None:
    """Check the first bytes of a file against its claimed container format."""
    with path.open("rb") as fh:
        head = fh.read(16)
    ext = extension.lower()
    if ext in (".mp4", ".mov"):
        if len(head) < 8 or head[4:8] not in (b"ftyp", b"moov", b"mdat", b"wide", b"free"):
            raise SecurityError("The file does not look like an MP4 or MOV video")
    elif ext in (".webm", ".mkv"):
        if head[:4] != b"\x1a\x45\xdf\xa3":
            raise SecurityError("The file does not look like a WEBM or MKV video")
    else:
        raise SecurityError(f"File type {ext} is not supported")


def check_extension(filename: str) -> str:
    ext = Path(filename or "").suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise SecurityError("Supported formats are MP4, MOV, WEBM and MKV")
    return ext


def safe_filename(name: str) -> str:
    keep = "".join(c for c in Path(name or "video").name if c.isalnum() or c in "._- ")
    return keep.strip() or "video"
