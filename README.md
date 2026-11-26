# AI Clipper

Turns a long video into several short, captioned clips. You pick the clip length, the number of clips, a theme (Funny, Gaming, and so on), and the orientation (landscape, vertical 9:16, or square). A language model chooses the moments. FFmpeg does all the cutting, framing, captions, and audio.

## Quick start (solo, on your own machine)

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows (macOS/Linux: source .venv/bin/activate)
pip install -r requirements.txt
pip install faster-whisper        # local transcription (optional, see below)
python -m clipper preflight       # checks FFmpeg, the database, the model, and RAM
python -m clipper serve           # then open http://127.0.0.1:8000
```

FFmpeg comes with `imageio-ffmpeg`, which is installed above. If you prefer a system FFmpeg, set `CLIPPER_FFMPEG_PATH`.

## Deployment modes

The mode is one setting, `CLIPPER_MODE`:

| Mode | Storage | Database | Sign-in | Default transcription |
|---|---|---|---|---|
| `local` (default) | Local disk | SQLite | Off, localhost only | Local Whisper (`small`) |
| `vps` | Local disk, or S3 with `CLIPPER_STORAGE=s3` | SQLite, or PostgreSQL with `CLIPPER_DATABASE_URL` | On | Local Whisper (`base`) |
| `aws` | S3 | SQLite or PostgreSQL | On | Cloud provider (Groq) |

Any single variable overrides the mode default. Copy `.env.example` to `.env` to set them.

## Configuration

Every variable is `CLIPPER_<NAME>`. The main ones:

- `CLIPPER_MASTER_KEY`: encrypts saved API keys. If unset, a key file is created in the data folder with restricted permissions. Keep it outside the database.
- `CLIPPER_DATA_DIR`, `CLIPPER_EFFECTS_DIR`, `CLIPPER_MODELS_DIR`
- `CLIPPER_WHISPER_MODEL`: `tiny`, `base`, `small`, `medium`, or `large-v3`
- `CLIPPER_UPLOAD_MAX_MB`, `CLIPPER_MAX_DURATION_SEC`
- `CLIPPER_WORKER_CONCURRENCY`: how many jobs run at once

## How it works

1. **Download or upload.** Videos are checked by file header, duration, and audio track.
2. **Transcribe.** Audio is extracted and transcribed with timestamps, including word timings when available.
3. **Choose settings.** Theme, length, count, orientation, resolution, captions, and mirror.
4. **Plan.** The transcript goes to the language models in priority order. Output is validated, retried once if malformed, and then sent to the next provider. If no model answers, a heuristic picks the densest speech.
5. **Render.** Each clip is cut, reframed, captioned, and mixed by FFmpeg, one job per clip.

The model only ever receives text. It never receives video or audio.

## Tests

```bash
pip install pytest
python -m pytest
```

The tests render real clips with FFmpeg. Transcription is replaced with a fake engine, so they need no model download and no network access.

## Legal

Only process content you own or have permission to use. Link downloads from video platforms can break their terms, so check those terms before any public launch. Sound effects and B-roll must be licensed for commercial use if the clips will be published or sold. The app records the license in each effect's manifest.
