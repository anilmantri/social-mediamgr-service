"""
Cloud storage — S3 / Cloudflare R2 (S3-compatible).
boto3 is synchronous. We run it in a thread pool to avoid
blocking the async event loop.
"""
import asyncio
import mimetypes
from functools import partial
from pathlib import Path

import structlog

from app.core.config import settings

log = structlog.get_logger(__name__)


class StorageService:
    def __init__(self) -> None:
        self._enabled = bool(
            settings.S3_ACCESS_KEY_ID and settings.S3_SECRET_ACCESS_KEY
        )
        self._client = None
        if self._enabled:
            import boto3
            kwargs: dict = {
                "aws_access_key_id": settings.S3_ACCESS_KEY_ID,
                "aws_secret_access_key": settings.S3_SECRET_ACCESS_KEY,
                "region_name": settings.S3_REGION,
            }
            if settings.S3_ENDPOINT_URL:
                kwargs["endpoint_url"] = settings.S3_ENDPOINT_URL
            self._client = boto3.client("s3", **kwargs)

        self._bucket = settings.S3_BUCKET_NAME

    async def upload_bytes(
            self, data: bytes, key: str, content_type: str = "application/octet-stream"
    ) -> str:
        if not self._enabled or not self._client:
            log.warning("storage_disabled_returning_placeholder", key=key)
            return f"http://localhost:9000/{self._bucket}/{key}"

        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(
                None,
                partial(
                    self._client.put_object,
                    Bucket=self._bucket,
                    Key=key,
                    Body=data,
                    ContentType=content_type,
                    CacheControl="public, max-age=31536000",
                ),
            )
            log.info("storage_upload_success", key=key, bytes=len(data))
            return self._build_url(key)
        except Exception as e:
            log.error("storage_upload_failed", key=key, error=str(e))
            raise

    async def upload_file(self, local_path: str | Path, key: str) -> str:
        if not self._enabled or not self._client:
            return f"http://localhost:9000/{self._bucket}/{key}"

        content_type, _ = mimetypes.guess_type(str(local_path))
        content_type = content_type or "application/octet-stream"
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(
                None,
                partial(
                    self._client.upload_file,
                    Filename=str(local_path),
                    Bucket=self._bucket,
                    Key=key,
                    ExtraArgs={
                        "ContentType": content_type,
                        "CacheControl": "public, max-age=31536000",
                    },
                ),
            )
            return self._build_url(key)
        except Exception as e:
            log.error("storage_upload_file_failed", key=key, error=str(e))
            raise

    def generate_presigned_url(self, key: str, expiry_seconds: int = 3600) -> str:
        if not self._enabled or not self._client:
            return f"http://localhost:9000/{self._bucket}/{key}"
        return self._client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self._bucket, "Key": key},
            ExpiresIn=expiry_seconds,
        )

    def _build_url(self, key: str) -> str:
        if settings.CDN_BASE_URL:
            return f"{settings.CDN_BASE_URL.rstrip('/')}/{key}"
        if settings.S3_ENDPOINT_URL:
            return f"{settings.S3_ENDPOINT_URL.rstrip('/')}/{self._bucket}/{key}"
        return f"https://{self._bucket}.s3.{settings.S3_REGION}.amazonaws.com/{key}"


_storage_service: StorageService | None = None


def get_storage_service() -> StorageService:
    global _storage_service
    if _storage_service is None:
        _storage_service = StorageService()
    return _storage_service