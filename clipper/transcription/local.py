"""Local Whisper through faster-whisper. Models are cached in the models folder, so they
are downloaded once."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from . import ProgressFn, Transcript, TranscriptionEngine, TranscriptionError

MODEL_SIZES = ("tiny", "base", "small", "medium", "large-v3")
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

    def __init__(self, model_name: str, device: str, models_dir: Path):
        if model_name not in MODEL_SIZES:
            raise TranscriptionError(f"Unknown Whisper model {model_name!r}. Choose one of {MODEL_SIZES}")
        self.model_name = model_name
        self.device = device
        self.models_dir = models_dir
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
        model = self.load()
        try:
            segments_iter, info = model.transcribe(
                str(audio_path), word_timestamps=True, vad_filter=True,
            )
            segments: list[dict] = []
            words: list[dict] = []
            total = max(1.0, float(info.duration or 1.0))
            for seg in segments_iter:
                segments.append({"start": float(seg.start), "end": float(seg.end), "text": seg.text.strip()})
                for w in (seg.words or []):
                    words.append({"start": float(w.start), "end": float(w.end), "word": w.word})
                if progress:
                    progress(min(1.0, float(seg.end) / total))
        except TranscriptionError:
            raise
        except Exception as exc:
            raise TranscriptionError(f"Local transcription failed: {exc}") from exc
        return Transcript(
            engine=f"{self.name}:{self.model_name}",
            language=getattr(info, "language", None),
            segments=[s for s in segments if s["text"]],
            words=words or None,
        )
