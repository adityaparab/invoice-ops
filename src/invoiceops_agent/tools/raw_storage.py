"""Async S3 storage for immutable content-addressed raw document bytes."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from time import perf_counter
from typing import TYPE_CHECKING, Protocol

from aiobotocore.config import AioConfig
from aiobotocore.session import get_session
from botocore.exceptions import BotoCoreError, ClientError

from invoiceops_agent.tools.ingestion_errors import IngestionUnavailable
from invoiceops_agent.tools.ingestion_schemas import RawDocument

if TYPE_CHECKING:
    from types_aiobotocore_s3.client import S3Client

logger = logging.getLogger(__name__)


class RawStorage(Protocol):
    async def put(self, document: RawDocument, *, trace_id: str) -> str: ...


class S3RawStorage:
    def __init__(self, client: "S3Client", bucket: str, timeout_seconds: float) -> None:
        self.client = client
        self.bucket = bucket
        self.timeout_seconds = timeout_seconds

    async def put(self, document: RawDocument, *, trace_id: str) -> str:
        started = perf_counter()
        try:
            async with asyncio.timeout(self.timeout_seconds):
                await self.client.put_object(
                    Bucket=self.bucket,
                    Key=document.object_key,
                    Body=document.body,
                    ContentType=document.content_type,
                    IfNoneMatch="*",
                )
        except ClientError as error:
            # A content hash names identical bytes. Never overwrite/delete a shared object.
            if error.response["Error"].get("Code") != "PreconditionFailed":
                raise IngestionUnavailable("Raw document storage is unavailable.") from error
        except (BotoCoreError, TimeoutError, OSError) as error:
            raise IngestionUnavailable("Raw document storage is unavailable.") from error
        logger.info(
            "raw_document_stored trace_id=%s bytes=%d duration_ms=%.3f",
            trace_id,
            len(document.body),
            (perf_counter() - started) * 1000,
        )
        return f"s3://{self.bucket}/{document.object_key}"


@asynccontextmanager
async def s3_storage(
    *, endpoint: str, access_key: str, secret_key: str, bucket: str, timeout_seconds: float
) -> AsyncIterator[S3RawStorage]:
    """Explicit credentials avoid ambient AWS discovery or metadata-network calls."""
    async with get_session().create_client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="us-east-1",
        config=AioConfig(
            connect_timeout=timeout_seconds,
            read_timeout=timeout_seconds,
            retries={"total_max_attempts": 1},
            s3={"addressing_style": "path"},
        ),
    ) as client:
        yield S3RawStorage(client, bucket, timeout_seconds)
