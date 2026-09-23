"""Explicit one-shot raw bucket provisioning, never performed in request transactions."""

import asyncio
import logging

from botocore.exceptions import BotoCoreError, ClientError
from pydantic import ValidationError

from invoiceops_agent.obs.logging import configure_logging
from invoiceops_agent.tools.raw_storage import s3_storage
from invoiceops_agent.tools.storage_settings import StorageSettings

logger = logging.getLogger(__name__)


async def provision_bucket(settings: StorageSettings) -> None:
    if (
        settings.minio_url is None
        or settings.minio_access_key is None
        or settings.minio_secret_key is None
    ):
        raise ValueError("Raw storage configuration is incomplete")
    async with (
        asyncio.timeout(settings.storage_timeout_seconds),
        s3_storage(
            endpoint=str(settings.minio_url),
            access_key=settings.minio_access_key.get_secret_value(),
            secret_key=settings.minio_secret_key.get_secret_value(),
            bucket=settings.raw_bucket,
            timeout_seconds=settings.storage_timeout_seconds,
        ) as storage,
    ):
        try:
            await storage.client.create_bucket(Bucket=settings.raw_bucket)
        except ClientError as error:
            if error.response["Error"].get("Code") != "BucketAlreadyOwnedByYou":
                raise
    logger.info("raw_bucket_ready")


def main() -> int:
    configure_logging()
    try:
        asyncio.run(provision_bucket(StorageSettings()))
    except (
        BotoCoreError,
        ClientError,
        ValidationError,
        ValueError,
        TimeoutError,
        OSError,
    ) as error:
        logger.error("raw_bucket_failed error_type=%s", type(error).__name__)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
