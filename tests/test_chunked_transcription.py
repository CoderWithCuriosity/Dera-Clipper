"""Long audio is transcribed in pieces. A fake Whisper model is used, so no model download is needed."""
import subprocess
from types import SimpleNamespace

from clipper.media import Ffmpeg
from clipper.transcription.local import LocalWhisper


class FakeModel:
    """Returns one segment at the start of every piece, so the piece offsets are visible."""

    def __init__(self, ffmpeg):
        self.ffmpeg = ffmpeg
        self.calls = []

    def transcribe(self, path, word_timestamps=False, vad_filter=False):
        self.calls.append(str(path))
        duration = self.ffmpeg.probe(path)["duration"]
        seg = SimpleNamespace(start=0.5, end=1.5, text=" hello there", words=None)
        return iter([seg]), SimpleNamespace(duration=duration, language="en")


def test_long_audio_is_split_and_offsets_are_added(ffmpeg_bin, sample_video, tmp_path):
    ff = Ffmpeg(ffmpeg_bin)
    audio = tmp_path / "audio.mp3"
    subprocess.run([ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-y", "-i", str(sample_video),
                    "-vn", "-ac", "1", "-ar", "16000", "-b:a", "32k", str(audio)], check=True)

    engine = LocalWhisper("base", "cpu", tmp_path / "models", ffmpeg=ff, chunk_seconds=15)
    model = FakeModel(ff)
    engine._model = model
    progress = []
    result = engine.transcribe(audio, progress=progress.append)

    # 40 seconds of audio in 15-second pieces: three pieces, one segment each.
    assert len(model.calls) == 3
    starts = [s["start"] for s in result.segments]
    assert abs(starts[0] - 0.5) < 0.1
    assert abs(starts[1] - (15.0 + 0.5)) < 0.1
    assert abs(starts[2] - (30.0 + 0.5)) < 0.1
    assert result.language == "en" and result.engine == "whisper_local:base"
    assert progress and progress[-1] <= 1.0 and progress == sorted(progress)
    # The temporary pieces are removed.
    assert not [p for p in tmp_path.iterdir() if p.name.startswith("chunks-")]


def test_without_ffmpeg_the_file_is_transcribed_in_one_pass(ffmpeg_bin, sample_video, tmp_path):
    ff = Ffmpeg(ffmpeg_bin)
    audio = tmp_path / "audio.mp3"
    subprocess.run([ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-y", "-i", str(sample_video),
                    "-vn", "-ac", "1", "-ar", "16000", "-b:a", "32k", str(audio)], check=True)
    engine = LocalWhisper("base", "cpu", tmp_path / "models")
    model = FakeModel(ff)
    engine._model = model
    result = engine.transcribe(audio)
    assert len(model.calls) == 1 and len(result.segments) == 1
