import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _ffmpeg_binary() -> str:
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


@pytest.fixture(scope="session")
def ffmpeg_bin():
    return _ffmpeg_binary()


@pytest.fixture(scope="session")
def sample_video(tmp_path_factory, ffmpeg_bin) -> Path:
    """A 40-second 640x360 test video with a tone, so the media engine has real input."""
    path = tmp_path_factory.mktemp("media") / "sample.mp4"
    subprocess.run(
        [ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc=size=640x360:rate=25:duration=40",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=40",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-shortest", str(path)],
        check=True,
    )
    return path


class FakeEngine:
    """Stands in for Whisper: one segment every 4 seconds, with word timings."""

    name = "fake"

    def __init__(self, duration: float = 40.0):
        self.duration = duration

    def check(self):
        return None

    def transcribe(self, audio_path, progress=None):
        from clipper.transcription import Transcript

        segments, words = [], []
        t = 0.0
        sentences = [
            "this is the funny part of the story",
            "and then everything went completely wrong",
            "nobody expected the ending to happen like that",
            "honestly that was the best moment of the whole day",
            "we laughed for an hour after that happened",
            "tell me you did not see that coming",
            "the final scene is where it all falls apart",
            "that is a lesson everyone should remember",
            "so keep watching because the next part is wild",
            "and that wraps up the entire episode today",
        ]
        i = 0
        while t < self.duration:
            text = sentences[i % len(sentences)]
            end = min(self.duration, t + 4.0)
            segments.append({"start": t, "end": end, "text": text})
            step = (end - t) / max(1, len(text.split()))
            for n, word in enumerate(text.split()):
                words.append({"start": t + n * step, "end": t + (n + 1) * step, "word": " " + word})
            t = end
            i += 1
            if progress:
                progress(min(1.0, t / self.duration))
        return Transcript(engine="fake", language="en", segments=segments, words=words)


@pytest.fixture()
def app_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIPPER_MODE", "local")
    monkeypatch.setenv("CLIPPER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CLIPPER_EFFECTS_DIR", str(ROOT / "effects"))
    monkeypatch.setenv("CLIPPER_RUN_WORKER", "true")
    monkeypatch.setenv("CLIPPER_JOB_MAX_ATTEMPTS", "1")
    monkeypatch.setenv("CLIPPER_MASTER_KEY", "test-master-key-for-tests")
    monkeypatch.delenv("CLIPPER_DATABASE_URL", raising=False)
    monkeypatch.delenv("CLIPPER_STORAGE", raising=False)
    return tmp_path


@pytest.fixture()
def client(app_env, monkeypatch):
    from fastapi.testclient import TestClient

    import clipper.pipeline as pipeline
    from clipper.api import create_app
    from clipper.config import load_settings

    monkeypatch.setattr(pipeline, "build_transcription_chain",
                        lambda svc, user_id, mode: [("local", FakeEngine(duration=40.0))])
    app = create_app(load_settings())
    with TestClient(app) as c:
        yield c


def wait_for(client, path, predicate, timeout=180):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = client.get(path).json()
        if predicate(last):
            return last
        time.sleep(0.5)
    raise AssertionError(f"Timed out waiting on {path}; last state: {last}")
