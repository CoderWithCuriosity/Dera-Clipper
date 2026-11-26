"""Startup and on-demand requirement checks. Each check says what is missing and how to fix it."""
from __future__ import annotations

import os
import platform
from typing import Optional

from .services import Services


def total_memory_gb() -> Optional[float]:
    try:
        if platform.system() == "Windows":
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(stat)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
            return stat.ullTotalPhys / (1024 ** 3)
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        return pages * page_size / (1024 ** 3)
    except (ValueError, OSError, AttributeError):
        return None


def run_checks(svc: Services) -> list[dict]:
    s = svc.settings
    checks: list[dict] = []

    def add(name: str, ok: bool, message: str, level: str = "error") -> None:
        checks.append({"name": name, "ok": ok, "level": "info" if ok else level, "message": message})

    # FFmpeg
    if svc.ffmpeg is None:
        add("FFmpeg", False, "FFmpeg was not found. Install it or run: pip install imageio-ffmpeg")
    else:
        add("FFmpeg", True, f"Found at {svc.ffmpeg.binary}")

    # Data folder and database
    try:
        probe = s.data_dir / ".write-test"
        probe.parent.mkdir(parents=True, exist_ok=True)
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        add("Data folder", True, f"Writable: {s.data_dir}")
    except OSError as exc:
        add("Data folder", False, f"Cannot write to {s.data_dir}: {exc}")

    try:
        svc.db.one("SELECT 1 AS ok")
        add("Database", True, f"Connected ({svc.db.kind})")
    except Exception as exc:
        add("Database", False, f"Cannot connect: {exc}")

    # Storage
    if s.storage == "s3":
        try:
            svc.storage.check()  # type: ignore[attr-defined]
            add("Storage bucket", True, f"Reachable: {s.s3_bucket}")
        except Exception as exc:
            add("Storage bucket", False, f"Bucket not reachable: {exc}")
    else:
        add("Storage", True, f"Local disk at {s.data_dir / 'objects'}")

    # Transcription
    if s.default_transcription_engine.startswith(("whisper_local", "local")):
        try:
            import faster_whisper  # noqa: F401

            add("Local transcription", True, "faster-whisper is installed")
        except ImportError:
            add("Local transcription", False, "faster-whisper is missing. Run: pip install faster-whisper")
        from .transcription.local import model_status

        status = model_status(s.models_dir, s.whisper_model)
        if status["downloaded"]:
            add("Whisper model", True, f"'{s.whisper_model}' is downloaded")
        else:
            add("Whisper model", False,
                f"'{s.whisper_model}' is not downloaded yet. It will download on first use, or use the Settings page.",
                level="warning")

    # Memory, by mode
    mem = total_memory_gb()
    if mem is not None:
        local_engine = s.default_transcription_engine.startswith(("whisper_local", "local"))
        if s.mode == "local":
            need = 8.0
        elif s.mode == "vps":
            need = 4.0 if local_engine else 1.0
        else:
            need = 2.0 if local_engine else 1.0
        ok = mem >= need
        add("Memory", ok,
            f"{mem:.1f} GB of RAM. Recommended for this mode: {need:.0f} GB"
            + ("" if ok else ". Use a cloud transcription provider on this machine."),
            level="warning")

    # Security posture
    if s.mode != "local" and not s.require_auth:
        add("Sign-in", False, "Multi-user modes should require sign-in. Set CLIPPER_REQUIRE_AUTH=true.", level="warning")
    elif s.mode == "local" and s.host not in ("127.0.0.1", "localhost"):
        add("Network", False, "Local mode is listening on the network. Only use this on a trusted LAN.", level="warning")

    key_ok = bool(os.environ.get("CLIPPER_MASTER_KEY")) or s.key_file.exists()
    add("Encryption key", key_ok, "Master key is set" if key_ok else "No master key yet; one is created on first use",
        level="warning")

    return checks
