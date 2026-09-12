"""MinIO / S3 helpers for knowledge documents and other blobs (spec 3)."""

from __future__ import annotations

from app.core.settings import get_settings
from shared.logging import get_logger

logger = get_logger(__name__)


class ObjectStoreError(Exception):
    def __init__(self, detail: str, *, status_code: int = 502) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


def _client():
    import aioboto3

    settings = get_settings()
    session = aioboto3.Session()
    return session.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url,
        aws_access_key_id=settings.s3_access_key_id.get_secret_value(),
        aws_secret_access_key=settings.s3_secret_access_key.get_secret_value(),
        region_name=settings.s3_region,
    )


async def put_object(key: str, data: bytes, content_type: str) -> None:
    settings = get_settings()
    try:
        async with _client() as s3:
            await s3.put_object(
                Bucket=settings.s3_bucket_recordings,
                Key=key,
                Body=data,
                ContentType=content_type,
            )
    except Exception as exc:
        logger.exception("object_store_put_failed", extra={"key": key})
        raise ObjectStoreError("could not store the file in object storage") from exc


async def get_object(key: str) -> bytes:
    settings = get_settings()
    try:
        async with _client() as s3:
            obj = await s3.get_object(Bucket=settings.s3_bucket_recordings, Key=key)
            return await obj["Body"].read()
    except Exception as exc:
        logger.exception("object_store_get_failed", extra={"key": key})
        raise ObjectStoreError("the stored file could not be read", status_code=404) from exc


async def delete_object(key: str) -> None:
    settings = get_settings()
    try:
        async with _client() as s3:
            await s3.delete_object(Bucket=settings.s3_bucket_recordings, Key=key)
    except Exception:
        logger.exception("object_store_delete_failed", extra={"key": key})
