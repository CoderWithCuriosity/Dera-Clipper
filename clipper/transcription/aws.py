"""AWS Transcribe as a transcription engine. The audio goes to the project's S3 bucket, AWS
writes the result JSON back to the bucket, and the timestamps are parsed into the common
Transcript shape. Credentials come from the instance role (IAM), not from app settings."""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Optional

from . import ProgressFn, Transcript, TranscriptionEngine, TranscriptionError

POLL_SECONDS = 5
TIMEOUT_SECONDS = 2 * 3600
PAUSE_BREAK = 1.0  # a pause this long starts a new caption segment


def parse_transcribe_document(doc: dict) -> Transcript:
    """Convert AWS Transcribe's result JSON into segments and word timings."""
    results = doc.get("results", {})
    words: list[dict] = []
    segments: list[dict] = []
    current: list[str] = []
    seg_start: Optional[float] = None
    last_end: Optional[float] = None

    def close_segment() -> None:
        nonlocal current, seg_start, last_end
        text = "".join(current).strip()
        if text and seg_start is not None and last_end is not None:
            segments.append({"start": seg_start, "end": last_end, "text": text})
        current, seg_start = [], None

    for item in results.get("items", []):
        alt = (item.get("alternatives") or [{}])[0]
        content = str(alt.get("content", ""))
        if item.get("type") == "pronunciation":
            start = float(item["start_time"])
            end = float(item["end_time"])
            if last_end is not None and start - last_end >= PAUSE_BREAK:
                close_segment()
            if seg_start is None:
                seg_start = start
            words.append({"start": start, "end": end, "word": " " + content})
            current.append(" " + content)
            last_end = end
        else:  # punctuation: attach it, and end the segment at a full stop
            if current:
                current[-1] = current[-1] + content
            if content in (".", "?", "!"):
                close_segment()
    close_segment()
    return Transcript(
        engine="aws_transcribe",
        language=results.get("language_code") or doc.get("language_code"),
        segments=segments,
        words=words or None,
    )


class AWSTranscribe(TranscriptionEngine):
    name = "aws_transcribe"

    def __init__(self, storage, region: Optional[str], bucket: str, prefix: str):
        self.storage = storage
        self.region = region
        self.bucket = bucket
        self.prefix = prefix

    def check(self) -> None:
        try:
            import boto3  # noqa: F401
        except ImportError as exc:
            raise TranscriptionError("boto3 is not installed. Run: pip install boto3") from exc

    def _client(self):
        import boto3

        return boto3.client("transcribe", region_name=self.region)

    def transcribe(self, audio_path: Path, progress: Optional[ProgressFn] = None) -> Transcript:
        self.check()
        token = uuid.uuid4().hex
        media_key = f"transcribe-in/{token}.mp3"
        output_key = f"transcribe-out/{token}.json"
        job_name = f"clipper-{token}"
        client = self._client()
        self.storage.put_file(media_key, audio_path)
        try:
            client.start_transcription_job(
                TranscriptionJobName=job_name,
                IdentifyLanguage=True,
                LanguageOptions=["en-US", "en-GB", "es-US", "fr-FR", "de-DE", "pt-BR"],
                MediaFormat="mp3",
                Media={"MediaFileUri": f"s3://{self.bucket}/{self.prefix}/{media_key}"},
                OutputBucketName=self.bucket,
                OutputKey=f"{self.prefix}/{output_key}",
            )
            deadline = time.time() + TIMEOUT_SECONDS
            while True:
                status = client.get_transcription_job(TranscriptionJobName=job_name)["TranscriptionJob"]
                state = status["TranscriptionJobStatus"]
                if state == "COMPLETED":
                    break
                if state == "FAILED":
                    raise TranscriptionError("AWS Transcribe failed: " + status.get("FailureReason", "unknown reason"))
                if time.time() > deadline:
                    raise TranscriptionError("AWS Transcribe took too long")
                if progress:
                    progress(0.5)
                time.sleep(POLL_SECONDS)
            local = self.storage.local_path(output_key)
            doc = json.loads(Path(local).read_text(encoding="utf-8"))
        except TranscriptionError:
            raise
        except Exception as exc:
            raise TranscriptionError(f"AWS Transcribe error: {exc}") from exc
        finally:
            try:
                client.delete_transcription_job(TranscriptionJobName=job_name)
            except Exception:
                pass
            for key in (media_key, output_key):
                try:
                    self.storage.delete(key)
                except Exception:
                    pass
        if progress:
            progress(1.0)
        return parse_transcribe_document(doc)
