"""S3 storage adapter. Works with AWS S3, MinIO, and Backblaze B2 (set CLIPPER_S3_ENDPOINT_URL).
Credentials come from the standard boto3 chain: on AWS, the instance's IAM role. No access
keys are stored in the app."""
from __future__ import annotations

import logging
import mimetypes
import shutil
from pathlib import Path
from typing import Optional

from .config import Settings

log = logging.getLogger("clipper.storage")
PRESIGN_SECONDS = 3600


class S3Storage:
    def __init__(self, settings: Settings):
        try:
            import boto3
        except ImportError as exc:
            raise RuntimeError("boto3 is required for S3 storage. Run: pip install boto3") from exc
        if not settings.s3_bucket:
            raise RuntimeError("CLIPPER_S3_BUCKET must be set for S3 storage")
        self.bucket = settings.s3_bucket
        self.prefix = settings.s3_prefix
        self.cache = settings.data_dir / "s3-cache"
        self.cache.mkdir(parents=True, exist_ok=True)
        from botocore.config import Config

        self.client = boto3.client(
            "s3", region_name=settings.s3_region, endpoint_url=settings.s3_endpoint_url,
            config=Config(signature_version="s3v4"),
        )

    def _key(self, key: str) -> str:
        parts = [p for p in key.replace("\\", "/").split("/") if p not in ("", ".", "..")]
        if not parts:
            raise ValueError("Empty storage key")
        return f"{self.prefix}/" + "/".join(parts)

    def check(self) -> None:
        self.client.head_bucket(Bucket=self.bucket)

    def put_file(self, key: str, src: Path) -> None:
        # Store the real content type. Without it, browsers refuse to play the video.
        content_type = mimetypes.guess_type(str(src))[0] or "application/octet-stream"
        self.client.upload_file(str(src), self.bucket, self._key(key), ExtraArgs={"ContentType": content_type})

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self.client.head_object(Bucket=self.bucket, Key=self._key(key))
            return True
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return False
            raise

    def local_path(self, key: str) -> Path:
        """Download the object into the local cache (once) and return its path. FFmpeg needs
        a real file, so every render works from the cache."""
        local = self.cache.joinpath(*[p for p in key.split("/") if p not in ("", ".", "..")])
        if local.exists():
            return local
        local.parent.mkdir(parents=True, exist_ok=True)
        tmp = local.with_suffix(local.suffix + ".part")
        try:
            self.client.download_file(self.bucket, self._key(key), str(tmp))
        except Exception as exc:
            tmp.unlink(missing_ok=True)
            raise FileNotFoundError(key) from exc
        tmp.replace(local)
        return local

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=self._key(key))
        local = self.cache.joinpath(*key.split("/"))
        if local.exists():
            local.unlink()

    def delete_prefix(self, prefix: str) -> None:
        full = self._key(prefix) + "/"
        paginator = self.client.get_paginator("list_objects_v2")
        batch: list[dict] = []
        for page in paginator.paginate(Bucket=self.bucket, Prefix=full):
            for obj in page.get("Contents", []):
                batch.append({"Key": obj["Key"]})
                if len(batch) == 1000:
                    self.client.delete_objects(Bucket=self.bucket, Delete={"Objects": batch})
                    batch = []
        if batch:
            self.client.delete_objects(Bucket=self.bucket, Delete={"Objects": batch})
        cached = self.cache.joinpath(*prefix.split("/"))
        if cached.is_dir():
            shutil.rmtree(cached, ignore_errors=True)

    def download_url(self, key: str, filename: str, attachment: bool = True) -> Optional[str]:
        disposition = f'attachment; filename="{filename}"' if attachment else f'inline; filename="{filename}"'
        return self.client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": self._key(key), "ResponseContentType": "video/mp4",
                    "ResponseContentDisposition": disposition},
            ExpiresIn=PRESIGN_SECONDS,
        )

    def list_keys(self, prefix: str) -> list[str]:
        out: list[str] = []
        full = self._key(prefix)
        for page in self.client.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=full):
            for obj in page.get("Contents", []):
                out.append(obj["Key"][len(self.prefix) + 1:])
        return sorted(out)
