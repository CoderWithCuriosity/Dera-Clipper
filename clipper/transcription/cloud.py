"""Cloud transcription through Whisper-compatible speech APIs (Groq, OpenAI). Audio is split
into chunks so each upload stays under provider limits, and the timestamps are merged
afterwards."""
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Optional

import httpx

from . import ProgressFn, Transcript, TranscriptionEngine, TranscriptionError, merge_chunks

CHUNK_SECONDS = 600  # 10 minutes of 32 kbit/s mono audio is about 2.4 MB, well under 25 MB

TRANSCRIPTION_PRESETS = {
    "groq_whisper": {
        "label": "Groq (Whisper)", "base_url": "https://api.groq.com/openai/v1",
        "model": "whisper-large-v3-turbo", "key_required": True,
        "docs": "https://console.groq.com/docs/rate-limits",
    },
    "openai_whisper": {
        "label": "OpenAI (Whisper)", "base_url": "https://api.openai.com/v1",
        "model": "whisper-1", "key_required": True,
        "docs": "https://platform.openai.com/docs/guides/rate-limits",
    },
    "aws_transcribe": {
        # No key: AWS credentials come from the instance role. Needs S3 storage.
        "label": "AWS Transcribe", "base_url": None, "model": "transcribe", "key_required": False,
        "docs": "https://aws.amazon.com/transcribe/pricing/",
    },
}


class WhisperAPI(TranscriptionEngine):
    def __init__(self, name: str, base_url: str, model: str, api_key: str, ffmpeg, timeout: float = 300.0):
        if not api_key:
            raise TranscriptionError(f"{name}: the API key is missing")
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.ffmpeg = ffmpeg
        self.timeout = timeout

    def check(self) -> None:
        return None

    def _transcribe_chunk(self, path: Path) -> Transcript:
        url = f"{self.base_url}/audio/transcriptions"
        data = {
            "model": self.model,
            "response_format": "verbose_json",
            "timestamp_granularities[]": ["word", "segment"],
        }
        try:
            with path.open("rb") as fh:
                resp = httpx.post(
                    url, headers={"Authorization": f"Bearer {self.api_key}"},
                    data=data, files={"file": (path.name, fh, "audio/mpeg")}, timeout=self.timeout,
                )
        except httpx.HTTPError as exc:
            raise TranscriptionError(f"{self.name}: network error ({exc.__class__.__name__})") from exc
        if resp.status_code == 429:
            raise TranscriptionError(f"{self.name}: rate limit reached")
        if resp.status_code in (401, 403):
            raise TranscriptionError(f"{self.name}: the API key was rejected")
        if resp.status_code >= 400:
            raise TranscriptionError(f"{self.name}: HTTP {resp.status_code} {resp.text[:160]}")
        body = resp.json()
        segments = [
            {"start": float(s["start"]), "end": float(s["end"]), "text": str(s.get("text", "")).strip()}
            for s in body.get("segments", []) if str(s.get("text", "")).strip()
        ]
        words = [
            {"start": float(w["start"]), "end": float(w["end"]), "word": str(w.get("word", ""))}
            for w in body.get("words", [])
        ] or None
        if not segments and body.get("text"):
            segments = [{"start": 0.0, "end": 0.0, "text": str(body["text"]).strip()}]
        return Transcript(engine=self.name, language=body.get("language"), segments=segments, words=words)

    def transcribe(self, audio_path: Path, progress: Optional[ProgressFn] = None) -> Transcript:
        with tempfile.TemporaryDirectory(prefix="chunks-") as tmp:
            chunks = self.ffmpeg.split_audio(audio_path, Path(tmp), CHUNK_SECONDS)
            if not chunks:
                raise TranscriptionError(f"{self.name}: the audio could not be split")
            parts: list[tuple[Transcript, float]] = []
            for n, (chunk, offset) in enumerate(chunks, start=1):
                parts.append((self._transcribe_chunk(chunk), offset))
                if progress:
                    progress(n / len(chunks))
        return merge_chunks(self.name, None, parts)
