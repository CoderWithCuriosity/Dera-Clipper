"""Local Whisper through faster-whisper. Models are cached in the models folder, so they
are downloaded once."""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Optional

from . import ProgressFn, Transcript, TranscriptionEngine, TranscriptionError

MODEL_SIZES = ("tiny", "base", "small", "medium", "large-v3")
CHUNK_SECONDS = 600  # ten-minute pieces keep memory use low on a small server
_SLOW_MODELS = ("medium", "large-v3")


def model_status(models_dir: Path, name: str) -> dict:
    """Report whether a model has been downloaded yet. faster-whisper stores each model in
    a folder whose name contains the model name."""
    found = False
    if models_dir.exists():
        found = any(p.is_dir() and f"faster-whisper-{name}" in p.name for p in models_dir.iterdir())
    return {"name": name, "downloaded": found, "slow_on_cpu": name in _SLOW_MODELS}


class LocalWhisper(TranscriptionEngine):
    name = "whisper_local"

    def __init__(self, model_name: str, device: str, models_dir: Path, ffmpeg=None,
                 chunk_seconds: int = CHUNK_SECONDS):
        if model_name not in MODEL_SIZES:
            raise TranscriptionError(f"Unknown Whisper model {model_name!r}. Choose one of {MODEL_SIZES}")
        self.model_name = model_name
        self.device = device
        self.models_dir = models_dir
        self.ffmpeg = ffmpeg          # when set, long audio is transcribed in pieces
        self.chunk_seconds = chunk_seconds
        self._model = None

    def check(self) -> None:
        try:
            import faster_whisper  # noqa: F401
        except ImportError as exc:
            raise TranscriptionError(
                "faster-whisper is not installed. Run: pip install faster-whisper"
            ) from exc

    def _resolve_device(self) -> tuple[str, str]:
        if self.device in ("cpu", "cuda"):
            device = self.device
        else:
            device = "cpu"
            try:
                import ctranslate2

                if ctranslate2.get_cuda_device_count() > 0:
                    device = "cuda"
            except Exception:
                device = "cpu"
        compute = "float16" if device == "cuda" else "int8"
        return device, compute

    def load(self):
        if self._model is None:
            self.check()
            from faster_whisper import WhisperModel

            device, compute = self._resolve_device()
            self.models_dir.mkdir(parents=True, exist_ok=True)
            # Downloads the model into models_dir the first time it is used.
            self._model = WhisperModel(
                self.model_name, device=device, compute_type=compute,
                download_root=str(self.models_dir),
            )
        return self._model

    def download(self) -> None:
        """Fetch the model without transcribing anything."""
        self.load()

    def transcribe(self, audio_path: Path, progress: Optional[ProgressFn] = None) -> Transcript:
        """Transcribe in pieces of chunk_seconds. Memory then depends on the piece length, not on
        the length of the video, so a two-hour video cannot exhaust a small server."""
        model = self.load()
        tmp: Optional[str] = None
        try:
            pieces: list[tuple[Path, float]] = [(audio_path, 0.0)]
            if self.ffmpeg is not None:
                tmp = tempfile.mkdtemp(prefix="chunks-", dir=str(audio_path.parent))
                split = self.ffmpeg.split_audio(audio_path, Path(tmp), self.chunk_seconds)
                if split:
                    pieces = self._with_offsets(split)
            segments, words, language = self._transcribe_pieces(model, pieces, progress)
        except TranscriptionError:
            raise
        except Exception as exc:
            raise TranscriptionError(f"Local transcription failed: {exc}") from exc
        finally:
            if tmp is not None:
                shutil.rmtree(tmp, ignore_errors=True)
        return Transcript(
            engine=f"{self.name}:{self.model_name}",
            language=language,
            segments=[s for s in segments if s["text"]],
            words=words or None,
        )

    def _with_offsets(self, split: list[tuple[Path, float]]) -> list[tuple[Path, float]]:
        """Cut points from ffmpeg land on frame boundaries, so each piece's start is the sum of
        the real durations before it, not a fixed multiple of chunk_seconds."""
        out: list[tuple[Path, float]] = []
        offset = 0.0
        for path, _ in split:
            out.append((path, offset))
            offset += float(self.ffmpeg.probe(path)["duration"])
        return out

    @staticmethod
    def _transcribe_pieces(model, pieces, progress):
        segments: list[dict] = []
        words: list[dict] = []
        language = None
        count = len(pieces)
        for index, (path, offset) in enumerate(pieces):
            seg_iter, info = model.transcribe(str(path), word_timestamps=True, vad_filter=True)
            language = language or getattr(info, "language", None)
            span = max(1.0, float(info.duration or 1.0))
            for seg in seg_iter:
                segments.append({"start": offset + float(seg.start), "end": offset + float(seg.end),
                                 "text": seg.text.strip()})
                for w in (seg.words or []):
                    words.append({"start": offset + float(w.start), "end": offset + float(w.end), "word": w.word})
                if progress:
                    progress(min(1.0, (index + min(1.0, float(seg.end) / span)) / count))
        return segments, words, language
