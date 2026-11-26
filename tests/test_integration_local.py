"""Integration test for real local Whisper transcription. It runs only when faster-whisper is
installed and the tiny model is already downloaded, so the normal test run stays fast and offline."""
import importlib.util
import subprocess
from pathlib import Path

import pytest

from clipper.config import ROOT

MODELS = ROOT / "data" / "models"


def _tiny_cached() -> bool:
    return MODELS.exists() and any("faster-whisper-tiny" in p.name for p in MODELS.iterdir())


pytestmark = pytest.mark.skipif(
    not (_tiny_cached() and importlib.util.find_spec("faster_whisper")),
    reason="needs faster-whisper and the tiny model in data/models (run the app once to download it)",
)


def _speech_clip(tmp_path: Path, ffmpeg_bin: str) -> Path:
    """Synthesize a short sentence with a tone-free voice. Uses Windows speech if available,
    otherwise skips. The file is generated in tmp_path, never committed."""
    wav = tmp_path / "speech.wav"
    script = (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        f"$s.SetOutputToWaveFile('{wav}'); "
        "$s.Speak('Welcome back everyone. My dog stole a sandwich right off the table.'); $s.Dispose()"
    )
    proc = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True)
    if proc.returncode != 0 or not wav.exists():  # no Windows speech synthesizer on this machine
        pytest.skip("speech synthesis failed")
    return wav


def test_local_whisper_transcribes_speech(tmp_path, ffmpeg_bin):
    from clipper.media import Ffmpeg
    from clipper.transcription.local import LocalWhisper

    wav = _speech_clip(tmp_path, ffmpeg_bin)
    audio = Ffmpeg(ffmpeg_bin).extract_audio(wav, tmp_path / "speech.mp3")
    transcript = LocalWhisper("tiny", "cpu", MODELS).transcribe(audio)
    text = " ".join(s["text"] for s in transcript.segments).lower()
    assert "sandwich" in text or "dog" in text
    assert transcript.words and transcript.words[0]["end"] > transcript.words[0]["start"]
    assert transcript.engine == "whisper_local:tiny"
