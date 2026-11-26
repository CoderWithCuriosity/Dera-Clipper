"""Media engine. FFmpeg does all cutting, framing, mirroring, captions and audio mixing.
AI is never involved here."""
from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

from .config import Settings

ORIENTATIONS = ("landscape", "vertical", "square")
RESOLUTIONS = ("1080p", "720p")
_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_AUDIO_RE = re.compile(r"Stream #\d+:\d+.*?: Audio:")


class MediaError(RuntimeError):
    """A media step failed. This is never retried automatically."""


@dataclass
class Effect:
    """One sound or B-roll placement inside a clip. Times are relative to the clip start."""
    path: Path
    kind: str            # "sound" or "broll"
    at: float
    duration: float = 0.0


def frame_size(orientation: str, resolution: str) -> tuple[int, int]:
    if orientation not in ORIENTATIONS:
        raise ValueError(f"orientation must be one of {ORIENTATIONS}")
    if resolution not in RESOLUTIONS:
        raise ValueError(f"resolution must be one of {RESOLUTIONS}")
    long_side = 1920 if resolution == "1080p" else 1280
    short_side = 1080 if resolution == "1080p" else 720
    if orientation == "landscape":
        return long_side, short_side          # 16:9
    if orientation == "vertical":
        return short_side, long_side          # 9:16, for Shorts, Reels, TikTok
    return short_side, short_side             # 1:1


def find_ffmpeg(settings: Settings) -> Optional[str]:
    if settings.ffmpeg_path and Path(settings.ffmpeg_path).exists():
        return settings.ffmpeg_path
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


class Ffmpeg:
    def __init__(self, binary: str, timeout: int = 60 * 60):
        self.binary = binary
        self.timeout = timeout

    def _run(self, args: Sequence[str], cwd: Optional[Path] = None, timeout: Optional[int] = None) -> subprocess.CompletedProcess:
        cmd = [self.binary, "-hide_banner", "-nostdin", "-y", *args]
        try:
            proc = subprocess.run(
                cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=timeout or self.timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise MediaError("FFmpeg took too long and was stopped") from exc
        return proc

    def _check(self, proc: subprocess.CompletedProcess, step: str) -> None:
        if proc.returncode != 0:
            tail = (proc.stderr or "").strip().splitlines()[-6:]
            raise MediaError(f"{step} failed: " + " | ".join(tail))

    def probe(self, path: Path) -> dict:
        """Duration in seconds and whether the file has an audio stream. Uses ffmpeg's
        own report, because the bundled binary does not include ffprobe."""
        proc = self._run(["-i", str(path)], timeout=120)
        text = proc.stderr or ""
        match = _DURATION_RE.search(text)
        if not match:
            raise MediaError("Could not read this file. It may be corrupt or not a video.")
        h, m, s = match.groups()
        duration = int(h) * 3600 + int(m) * 60 + float(s)
        return {"duration": duration, "has_audio": bool(_AUDIO_RE.search(text))}

    def extract_audio(self, src: Path, dst: Path) -> Path:
        """Mono, 16 kHz, 32 kbit/s MP3. Small enough for most cloud upload limits."""
        proc = self._run(["-i", str(src), "-vn", "-ac", "1", "-ar", "16000", "-b:a", "32k", str(dst)])
        self._check(proc, "Audio extraction")
        return dst

    def split_audio(self, src: Path, out_dir: Path, chunk_seconds: int) -> list[tuple[Path, float]]:
        """Split audio into fixed chunks. Returns (path, offset in seconds) pairs so the
        timestamps can be merged after transcription."""
        out_dir.mkdir(parents=True, exist_ok=True)
        pattern = out_dir / "chunk_%04d.mp3"
        proc = self._run([
            "-i", str(src), "-f", "segment", "-segment_time", str(chunk_seconds),
            "-reset_timestamps", "1", "-c", "copy", str(pattern),
        ])
        self._check(proc, "Audio splitting")
        chunks = sorted(out_dir.glob("chunk_*.mp3"))
        return [(path, i * float(chunk_seconds)) for i, path in enumerate(chunks)]

    def render_clip(
        self,
        src: Path,
        dst: Path,
        start: float,
        end: float,
        *,
        work_dir: Path,
        orientation: str = "landscape",
        resolution: str = "1080p",
        mirror: bool = False,
        captions: Optional[str] = None,
        has_audio: bool = True,
        effects: Sequence[Effect] = (),
        ducking_source_is_speech: bool = True,
    ) -> Path:
        """Cut [start, end] from src and produce a finished MP4 in the chosen frame size."""
        duration = max(0.1, end - start)
        width, height = frame_size(orientation, resolution)
        work_dir.mkdir(parents=True, exist_ok=True)

        inputs: list[str] = ["-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(src)]
        index = 1
        chains: list[str] = []

        # Video: scale to cover the frame, centre-crop, optional mirror, optional captions.
        video = f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},setsar=1"
        if mirror:
            video += ",hflip"
        if captions:
            (work_dir / "captions.ass").write_text(captions, encoding="utf-8")
            video += ",subtitles=captions.ass"
        chains.append(video + "[vbase]")
        last_video = "[vbase]"

        broll = [e for e in effects if e.kind == "broll"]
        sounds = [e for e in effects if e.kind == "sound"]

        for n, effect in enumerate(broll):
            inputs += ["-i", str(effect.path)]
            label_in = f"[{index}:v]"
            shown = max(0.5, min(effect.duration or 3.0, 3.0))
            box_w = int(width * 0.38)
            chains.append(
                f"{label_in}trim=0:{shown:.3f},setpts=PTS-STARTPTS,"
                f"scale={box_w}:-2,format=yuva420p[b{n}]"
            )
            out = f"[vb{n}]"
            # Top third of the frame, so B-roll never covers the captions at the bottom.
            x = width - box_w - int(width * 0.04)
            y = int(height * 0.06)
            chains.append(
                f"{last_video}[b{n}]overlay={x}:{y}:enable='between(t,{effect.at:.3f},{effect.at + shown:.3f})'{out}"
            )
            last_video = out
            index += 1
        filter_parts = list(chains)
        map_audio: list[str] = []

        if has_audio and sounds:
            # The speech feeds two places: the main mix, and the key that ducks the effects.
            audio_chain = ["[0:a]asplit=2[a_main][a_key]"]
            labels: list[str] = []
            for n, effect in enumerate(sounds):
                inputs += ["-i", str(effect.path)]
                delay_ms = int(max(0.0, effect.at) * 1000)
                audio_chain.append(
                    f"[{index}:a]atrim=0:{max(0.5, effect.duration or 2.0):.3f},asetpts=PTS-STARTPTS,"
                    f"adelay={delay_ms}|{delay_ms},volume=0.55[s{n}]"
                )
                labels.append(f"[s{n}]")
                index += 1
            audio_chain.append("".join(labels) + f"amix=inputs={len(labels)}:duration=longest:normalize=0[sfx]")
            # Automatic ducking: the effects drop while the speech is loud.
            audio_chain.append("[sfx][a_key]sidechaincompress=threshold=0.04:ratio=6:attack=10:release=300[sfxd]")
            audio_chain.append("[a_main][sfxd]amix=inputs=2:duration=first:weights=1 1:normalize=0[aout]")
            filter_parts += audio_chain
            map_audio = ["-map", "[aout]"]
        elif has_audio:
            map_audio = ["-map", "0:a?"]

        cmd = ["-filter_complex", ";".join(filter_parts), "-map", last_video]
        cmd = [*inputs, *cmd]
        cmd += map_audio
        cmd += [
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
        ]
        if has_audio:
            cmd += ["-c:a", "aac", "-b:a", "160k"]
        else:
            cmd += ["-an"]
        cmd += ["-movflags", "+faststart", str(dst)]

        proc = self._run(cmd, cwd=work_dir, timeout=max(600, int(duration * 20)))
        self._check(proc, "Clip rendering")
        return dst


# -- captions --------------------------------------------------------------------

def _ass_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _ass_text(text: str) -> str:
    return text.replace("\\", "/").replace("{", "(").replace("}", ")").replace("\n", " ").strip()


def build_captions_ass(
    segments: Sequence[dict],
    words: Optional[Sequence[dict]],
    clip_start: float,
    clip_end: float,
    width: int,
    height: int,
    style: str = "classic",
) -> Optional[str]:
    """Build an ASS subtitle file for one clip. Word timings are used when present,
    grouped into short caption lines; otherwise segment text is used."""
    lines: list[tuple[float, float, str]] = []
    if words:
        bucket: list[dict] = []
        for w in words:
            if w["end"] <= clip_start or w["start"] >= clip_end:
                continue
            bucket.append(w)
            span = bucket[-1]["end"] - bucket[0]["start"]
            if len(bucket) >= 5 or span >= 2.4:
                lines.append((bucket[0]["start"], bucket[-1]["end"], " ".join(x["word"].strip() for x in bucket)))
                bucket = []
        if bucket:
            lines.append((bucket[0]["start"], bucket[-1]["end"], " ".join(x["word"].strip() for x in bucket)))
    else:
        for seg in segments:
            if seg["end"] <= clip_start or seg["start"] >= clip_end:
                continue
            text = seg["text"].strip()
            if text:
                lines.append((max(seg["start"], clip_start), min(seg["end"], clip_end), text))

    if not lines:
        return None

    font_size = max(28, round(height * 0.056))
    margin_v = round(height * (0.18 if height > width else 0.08))
    style_line = _caption_style_line(style, font_size, margin_v)

    header = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        style_line,
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    events = [
        f"Dialogue: 0,{_ass_time(a - clip_start)},{_ass_time(b - clip_start)},Default,,0,0,0,,{_ass_text(t)}"
        for a, b, t in lines
    ]
    return "\n".join(header + events) + "\n"


# Caption styles. Each one is a single ASS style line, so new looks can be added here.
_CAPTION_STYLES = {
    # white text, black outline
    "classic": "&H00FFFFFF,&H000000FF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,4,1,2",
    # yellow text, thick black outline, slight shadow
    "bold": "&H0000FFFF,&H000000FF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,6,2,2",
    # white text on a translucent black box
    "boxed": "&H00FFFFFF,&H000000FF,&H00000000,&HA0000000,-1,0,0,0,100,100,0,0,3,0,0,2",
}


def caption_styles() -> list[str]:
    return list(_CAPTION_STYLES)


def _caption_style_line(style: str, font_size: int, margin_v: int) -> str:
    colours = _CAPTION_STYLES.get(style, _CAPTION_STYLES["classic"])
    return f"Style: Default,Arial,{font_size},{colours},60,60,{margin_v},1"
