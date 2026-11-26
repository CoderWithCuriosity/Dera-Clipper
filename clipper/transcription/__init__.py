"""Transcription adapters. Every engine returns the same Transcript shape, with timestamps
on every segment and word-level timings when the engine provides them."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional


class TranscriptionError(RuntimeError):
    """Transcription failed. Engine-side failures may be retried by the job runner."""

    retryable = False


@dataclass
class Transcript:
    engine: str
    language: Optional[str]
    segments: list[dict] = field(default_factory=list)   # {start, end, text}
    words: Optional[list[dict]] = None                  # {start, end, word}

    def to_dict(self) -> dict:
        return {"engine": self.engine, "language": self.language,
                "segments": self.segments, "words": self.words}


ProgressFn = Callable[[float], None]


class TranscriptionEngine:
    name = "base"

    def transcribe(self, audio_path: Path, progress: Optional[ProgressFn] = None) -> Transcript:
        raise NotImplementedError

    def check(self) -> None:
        """Raise TranscriptionError if the engine cannot run (missing package, bad key...)."""


def merge_chunks(engine: str, language: Optional[str], parts: list[tuple[Transcript, float]]) -> Transcript:
    """Join transcripts of consecutive audio chunks, shifting each by its offset."""
    segments: list[dict] = []
    words: list[dict] = []
    has_words = True
    for transcript, offset in parts:
        for seg in transcript.segments:
            segments.append({"start": seg["start"] + offset, "end": seg["end"] + offset, "text": seg["text"]})
        if transcript.words is None:
            has_words = False
        else:
            for w in transcript.words:
                words.append({"start": w["start"] + offset, "end": w["end"] + offset, "word": w["word"]})
        language = language or transcript.language
    return Transcript(engine=engine, language=language, segments=segments,
                      words=words if has_words and words else None)


def build_local_engine(settings) -> TranscriptionEngine:
    from ..media import Ffmpeg, find_ffmpeg
    from .local import LocalWhisper

    binary = find_ffmpeg(settings)
    return LocalWhisper(model_name=settings.whisper_model, device=settings.whisper_device,
                        models_dir=settings.models_dir, ffmpeg=Ffmpeg(binary) if binary else None)
