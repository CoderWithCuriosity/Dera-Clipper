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

## Providers

Language models (picking clips): Groq, Google Gemini, OpenAI, DeepSeek, OpenRouter, Anthropic Claude, Ollama (local, free), or any OpenAI-compatible endpoint.

Transcription: local Whisper (default), Groq Whisper, or OpenAI Whisper. In cloud mode, cloud providers are tried by priority and local Whisper is the final fallback.

Free-tier limits change often, so the app does not hard-code them. Each provider card links to its docs and shows the limits it reported on the last response. Usage counters show requests, tokens, and failures for the day.

## Effect library

```
effects/
  funny/
    manifest.json      display name, description, and one entry per file
    sfx/               sound effects
    broll/             short reaction clips
```

Each manifest entry has `file`, `kind` (`sound` or `broll`), optional `tags`, `speech` (true if the file contains speech, which is transcribed once and cached by content hash), `license`, and `source`. Add files, update the manifest, and click **Rescan library** in Setup.

To create placeholder effects for testing: `python scripts/make_placeholder_effects.py`. They are generated tones and colour clips with no third-party license.

Placements from the language model are checked: they must name a real effect, sit on a sentence end or pause, stay at least two seconds apart, and fit the clip. Sound is ducked automatically while someone speaks. If no model answers, effects are placed randomly at sentence ends, up to the per-clip limit (`CLIPPER_MAX_EFFECTS_PER_CLIP`, default 4).

## Clips, captions, and results

- **Orientation:** landscape 16:9, vertical 9:16 (Shorts, Reels, TikTok), or square 1:1. The frame is centre-cropped from the source. Resolution is 1080p or 720p.
- **Caption styles:** classic, bold, boxed, karaoke (each word highlights as it is spoken), or none.
- **Regenerate one clip** with a different caption style, orientation, or mirror setting, without redoing the others.
- **Results and analytics:** enter views and likes on each clip. The Analytics page shows average views by score band, length, theme, and whether the clip had effects, plus the top five clips.
- **Batches:** create a batch, save one set of clip settings, then upload or link many videos. Each video starts as soon as its transcript is ready. A video that is too short for the clip length is reported instead of failing silently.

## Multi-user accounts

In `vps` and `aws` modes, sign-in is on. The first account you create becomes the administrator. Set `CLIPPER_ADMIN_EMAIL` to choose the administrator by email instead, and `CLIPPER_ALLOW_REGISTRATION=false` to close sign-up after your accounts are made.

- Passwords are hashed with scrypt. Passwords need at least 10 characters.
- Access tokens last 15 minutes. Refresh tokens last 30 days, live in an HttpOnly cookie, and rotate on every use. Reusing an old refresh token signs the account out everywhere.
- Each user sees only their own projects, providers, and settings. Effects and the transcription model are shared.
- Limits per user: projects per day (`CLIPPER_MAX_PROJECTS_PER_USER_PER_DAY`), storage (`CLIPPER_USER_STORAGE_QUOTA_MB`), API requests per minute, and running jobs (`CLIPPER_MAX_RUNNING_JOBS_PER_USER`, so one heavy user cannot hold every worker). Sign-in attempts are limited to 10 per 15 minutes per address and email.
- The **Admin** page lists users (change roles, disable accounts), shows usage, and starts backups.

## Deploying

**VPS (Hetzner, DigitalOcean, Contabo, Linode).** Install Python 3.12, then:

```bash
sudo useradd --system --home /opt/clipper clipper
sudo git clone <this repo> /opt/clipper && cd /opt/clipper
sudo -u clipper python3 -m venv .venv
sudo -u clipper .venv/bin/pip install -r requirements.txt faster-whisper boto3 "psycopg[binary]"
sudo mkdir -p /etc/clipper /var/lib/clipper && sudo cp deploy/vps.env.example /etc/clipper/clipper.env
sudo chmod 600 /etc/clipper/clipper.env && sudo chown -R clipper /var/lib/clipper
```

Edit `/etc/clipper/clipper.env` and set `CLIPPER_MASTER_KEY`. Then install `deploy/clipper.service` and put `deploy/Caddyfile` in front of it for HTTPS. Caddy issues the certificate automatically. Run `python -m clipper preflight` as the `clipper` user to confirm everything is found.

**AWS (EC2).** Attach an IAM role to the instance with permissions on one bucket (`s3:GetObject`, `s3:PutObject`, `s3:DeleteObject`, `s3:ListBucket`) and on AWS Transcribe (`transcribe:StartTranscriptionJob`, `transcribe:GetTranscriptionJob`, `transcribe:DeleteTranscriptionJob`, `transcribe:ListTranscriptionJobs`). Set `CLIPPER_MODE=aws`, `CLIPPER_S3_BUCKET`, and `CLIPPER_S3_REGION`. No access keys go in the app. Micro instances (1 GB RAM) should use a cloud transcription provider, which is the default in this mode. The included `Dockerfile` runs the same app in a container. Mount a volume at `/data`.

**Storage.** `CLIPPER_STORAGE=s3` works with AWS S3, MinIO, and Backblaze B2. For the last two, set `CLIPPER_S3_ENDPOINT_URL`. Rendering downloads each source into a local cache first, because FFmpeg needs a real file.

**Database.** SQLite is the default. For more users, set `CLIPPER_DATABASE_URL=postgresql://...` and install `psycopg[binary]`.

**Backups.** `python -m clipper backup` writes a zip to the `backups` folder in the data directory. It holds a consistent database copy (SQLite online backup, or `pg_dump` for PostgreSQL), the settings without secrets, and the effect manifests. Backups run every 24 hours by default (`CLIPPER_BACKUP_EVERY_HOURS`) and the 14 most recent are kept. **Backups never include the master key.** Keep a copy of `CLIPPER_MASTER_KEY` somewhere safe and separate. Without it, saved API keys cannot be decrypted after a restore.

**Retention.** Raw uploads are removed after `CLIPPER_UPLOAD_RETENTION_DAYS` (default 7). Clips are kept until the user deletes them. Temporary work folders from crashed jobs are cleared daily.

## What has been tested

- Unit and end-to-end tests with real FFmpeg renders: 77 tests, run with `python -m pytest` (one PostgreSQL test is skipped until a scratch database is provided).
- Real speech transcribed with local Whisper (`tiny`), then a vertical clip rendered, checked end to end.
- S3 storage and the full pipeline on S3, using a mocked S3 (moto).
- Not verified against live services in this environment: PostgreSQL (the code path exists and runs when `CLIPPER_TEST_PG_URL` points at a scratch database), real AWS Transcribe (the result parser is tested on a sample document), real cloud LLM and Whisper APIs (adapters are tested with mocked HTTP), and the Docker image build.

## Tests

```bash
pip install pytest
python scripts/make_placeholder_effects.py   # creates the placeholder sound and B-roll files (git-ignored)
python -m pytest
```

The tests render real clips with FFmpeg. Transcription is replaced with a fake engine, so they need no model download and no network access.

## Legal

Only process content you own or have permission to use. Link downloads from video platforms can break their terms, so check those terms before any public launch. Sound effects and B-roll must be licensed for commercial use if the clips will be published or sold. The app records the license in each effect's manifest.
