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


@dataclass
class Logo:
    """A brand logo placed on the frame. Opacity is 0 to 100, size is a percentage of the
    frame width, and x and y are 0 to 100 across the free space (see clipper/branding.py)."""
    path: Path
    opacity: float = 80
    size: float = 15
    x: float = 97
    y: float = 3


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

    def image_to_png(self, src: Path, dst: Path, max_width: int = 1200) -> Path:
        """Convert a logo to PNG (keeping transparency), no wider than max_width."""
        proc = self._run(["-i", str(src), "-vf", f"scale='min({max_width},iw)':-2", "-frames:v", "1", str(dst)],
                         timeout=120)
        if proc.returncode != 0 or not dst.exists():
            raise MediaError("That image could not be read")
        return dst

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
        logo: Optional[Logo] = None,
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
        chains.append(video + "[vbase]")
        last_video = "[vbase]"

        broll = [e for e in effects if e.kind == "broll"]
        sounds = [e for e in effects if e.kind == "sound"]

        for n, effect in enumerate(broll):
            inputs += ["-i", str(effect.path)]
            label_in = f"[{index}:v]"
            shown = max(0.5, min(effect.duration or 3.0, 3.0))
            chains.append(
                f"{label_in}trim=0:{shown:.3f},setpts=PTS-STARTPTS,"
                f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},setsar=1[b{n}]"
            )
            out = f"[vb{n}]"
            # The B-roll fills the frame for a moment. Captions are drawn after it, so they stay visible.
            chains.append(
                f"{last_video}[b{n}]overlay=0:0:enable='between(t,{effect.at:.3f},{effect.at + shown:.3f})'{out}"
            )
            last_video = out
            index += 1
        if captions:
            (work_dir / "captions.ass").write_text(captions, encoding="utf-8")
            chains.append(f"{last_video}subtitles=captions.ass[vcap]")
            last_video = "[vcap]"

        if logo is not None:
            # The logo goes last, so it stays on top of the captions and B-roll. Its width is a
            # share of the frame; x and y slide it across the free space, so 0 and 100 touch the edges.
            inputs += ["-i", str(logo.path)]
            logo_w = max(16, round(width * logo.size / 100))
            alpha = max(0.0, min(1.0, logo.opacity / 100))
            chains.append(f"[{index}:v]format=rgba,scale={logo_w}:-2,colorchannelmixer=aa={alpha:.3f}[lg]")
            chains.append(
                f"{last_video}[lg]overlay=x='(W-w)*{logo.x / 100:.4f}':y='(H-h)*{logo.y / 100:.4f}'[vlogo]"
            )
            last_video = "[vlogo]"
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

    def render_mix(
        self,
        src: Path,
        dst: Path,
        ranges: Sequence[tuple[float, float]],
        *,
        work_dir: Path,
        orientation: str = "landscape",
        resolution: str = "1080p",
        mirror: bool = False,
        captions: Optional[str] = None,
        has_audio: bool = True,
        logo: Optional[Logo] = None,
    ) -> Path:
        """Join several moments from one video into one clip, in the order given. Each moment
        is cut and framed first, the parts are joined without re-encoding, and the joined clip
        then gets captions and the logo. Captions must use the joined timeline (see
        build_mix_captions_ass)."""
        if not ranges:
            raise MediaError("There are no moments to join")
        width, height = frame_size(orientation, resolution)
        parts_dir = work_dir / "parts"
        parts_dir.mkdir(parents=True, exist_ok=True)
        frame = f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},setsar=1"
        if mirror:
            frame += ",hflip"

        parts: list[Path] = []
        total = 0.0
        for n, (start, end) in enumerate(ranges):
            duration = max(0.1, end - start)
            part = parts_dir / f"part_{n:02d}.mp4"
            args = ["-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(src), "-map", "0:v:0", "-vf", frame,
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p"]
            if has_audio:
                args += ["-map", "0:a:0?", "-c:a", "aac", "-b:a", "160k"]
            else:
                args += ["-an"]
            args += [str(part)]
            self._check(self._run(args, cwd=parts_dir, timeout=max(600, int(duration * 20))), "Moment cutting")
            parts.append(part)
            total += duration

        listing = parts_dir / "parts.txt"
        listing.write_text("".join(f"file '{p.name}'\n" for p in parts), encoding="utf-8")
        joined = work_dir / "joined.mp4"
        proc = self._run(["-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy",
                          "-movflags", "+faststart", str(joined)], cwd=parts_dir, timeout=600)
        self._check(proc, "Joining moments")

        # The frame is already right, so only captions and the logo are added on this pass.
        return self.render_clip(
            joined, dst, 0.0, total, work_dir=work_dir, orientation=orientation, resolution=resolution,
            mirror=False, captions=captions, has_audio=has_audio, logo=logo,
        )


# -- captions --------------------------------------------------------------------

def _ass_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _ass_text(text: str) -> str:
    return text.replace("\\", "/").replace("{", "(").replace("}", ")").replace("\n", " ").strip()


def _caption_lines(segments, words, clip_start: float, clip_end: float, karaoke: bool) -> list[tuple[float, float, str]]:
    """Caption lines for one clip. Text is escaped here, once, so karaoke tags survive."""
    lines: list[tuple[float, float, str]] = []
    if words:
        bucket: list[dict] = []

        def flush() -> None:
            if not bucket:
                return
            if karaoke:
                # {\kN}: N centiseconds for each word, so the highlight moves with the speech.
                parts = []
                for x in bucket:
                    cs = max(1, round((x["end"] - x["start"]) * 100))
                    parts.append("{\\k%d}%s" % (cs, _ass_text(x["word"].strip())))
                text = " ".join(parts)
            else:
                text = " ".join(_ass_text(x["word"].strip()) for x in bucket)
            lines.append((bucket[0]["start"], bucket[-1]["end"], text))
            bucket.clear()

        for w in words:
            if w["end"] <= clip_start or w["start"] >= clip_end:
                continue
            bucket.append(w)
            if len(bucket) >= 5 or bucket[-1]["end"] - bucket[0]["start"] >= 2.4:
                flush()
        flush()
    else:
        for seg in segments:
            if seg["end"] <= clip_start or seg["start"] >= clip_end:
                continue
            text = seg["text"].strip()
            if text:
                lines.append((max(seg["start"], clip_start), min(seg["end"], clip_end), _ass_text(text)))
    return lines


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
    grouped into short caption lines; otherwise segment text is used. The karaoke style
    highlights each word as it is spoken, and needs word timings."""
    return build_mix_captions_ass(segments, words, [(clip_start, clip_end)], width, height, style)


def build_mix_captions_ass(
    segments: Sequence[dict],
    words: Optional[Sequence[dict]],
    ranges: Sequence[tuple[float, float]],
    width: int,
    height: int,
    style: str = "classic",
) -> Optional[str]:
    """Captions for one or more moments joined into one clip. Each range is a moment in the
    source video. Its captions are moved onto the joined timeline, one moment after another."""
    if style not in _CAPTION_STYLES:
        style = "classic"
    karaoke = style == "karaoke" and bool(words)
    lines: list[tuple[float, float, str]] = []
    offset = 0.0
    for clip_start, clip_end in ranges:
        for a, b, text in _caption_lines(segments, words, clip_start, clip_end, karaoke):
            a_in = min(max(a, clip_start), clip_end)
            b_in = min(max(b, clip_start), clip_end)
            lines.append((offset + a_in - clip_start, offset + b_in - clip_start, text))
        offset += max(0.0, clip_end - clip_start)
    if not lines:
        return None
    return _ass_document(lines, width, height, style)


def _ass_document(lines: list[tuple[float, float, str]], width: int, height: int, style: str) -> str:
    """The ASS file for finished caption lines (times already on the clip timeline)."""
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
        f"Dialogue: 0,{_ass_time(a)},{_ass_time(b)},Default,,0,0,0,,{t}"
        for a, b, t in lines
    ]
    return "\n".join(header + events) + "\n"


# Caption styles. Each one is a single ASS style line, so new looks can be added here.
# Colours are ASS &HAABBGGRR values. For karaoke, PrimaryColour is the highlight and
# SecondaryColour is the colour of words not yet spoken.
_CAPTION_STYLES = {
    # white text, black outline
    "classic": "&H00FFFFFF,&H000000FF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,4,1,2",
    # yellow text, thick black outline, slight shadow
    "bold": "&H0000FFFF,&H000000FF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,6,2,2",
    # white text on a translucent black box
    "boxed": "&H00FFFFFF,&H000000FF,&H00000000,&HA0000000,-1,0,0,0,100,100,0,0,3,0,0,2",
    # each word turns yellow as it is spoken; unspoken words are white
    "karaoke": "&H0000FFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,5,1,2",
}


def caption_styles() -> list[str]:
    return list(_CAPTION_STYLES)


def _caption_style_line(style: str, font_size: int, margin_v: int) -> str:
    colours = _CAPTION_STYLES.get(style, _CAPTION_STYLES["classic"])
    return f"Style: Default,Arial,{font_size},{colours},60,60,{margin_v},1"
