"""Read only the configured bucket's immutable content-addressed document objects."""

import asyncio
import logging
from time import perf_counter
from typing import TYPE_CHECKING, Protocol
from uuid import UUID

from botocore.exceptions import BotoCoreError, ClientError

from invoiceops_agent.schemas.documents import DocumentReference
from invoiceops_agent.tools.document_errors import DocumentError, DocumentUnavailable
from invoiceops_agent.tools.document_settings import DocumentSettings
from invoiceops_agent.tools.documents import read_document
from invoiceops_agent.tools.ingestion_errors import IngestionError
from invoiceops_agent.tools.ingestion_schemas import RawDocument

if TYPE_CHECKING:
    from types_aiobotocore_s3.client import S3Client

logger = logging.getLogger(__name__)


class DocumentReader(Protocol):
    async def read(
        self, reference: DocumentReference, *, run_id: UUID, trace_id: str
    ) -> RawDocument: ...


class S3DocumentReader:
    """The injected S3 client remains owned by its existing async context manager."""

    def __init__(self, client: "S3Client", bucket: str, settings: DocumentSettings) -> None:
        self._client = client
        self._bucket = bucket
        self._settings = settings

    async def read(
        self, reference: DocumentReference, *, run_id: UUID, trace_id: str
    ) -> RawDocument:
        started = perf_counter()
        key = f"sha256/{reference.content_hash[:2]}/{reference.content_hash}"
        if reference.raw_ref != f"s3://{self._bucket}/{key}":
            raise DocumentError(run_id=run_id, trace_id=trace_id)
        try:
            async with asyncio.timeout(self._settings.storage_timeout_seconds):
                response = await self._client.get_object(Bucket=self._bucket, Key=key)
                async with response["Body"] as body:
                    if (
                        response.get("ContentLength", -1) <= 0
                        or response.get("ContentLength", -1) > self._settings.max_document_bytes
                        or response.get("ContentType") != reference.content_type
                    ):
                        raise DocumentError(run_id=run_id, trace_id=trace_id)
                    document = await read_document(
                        body, reference.content_type, max_bytes=self._settings.max_document_bytes
                    )
                if (
                    document.content_hash != reference.content_hash
                    or len(document.body) != response["ContentLength"]
                ):
                    raise DocumentError(run_id=run_id, trace_id=trace_id)
        except IngestionError:
            raise DocumentError(run_id=run_id, trace_id=trace_id) from None
        except (BotoCoreError, ClientError, TimeoutError, OSError):
            logger.error("raw_document_read_failed run_id=%s trace_id=%s", run_id, trace_id)
            raise DocumentUnavailable(run_id=run_id, trace_id=trace_id) from None
        logger.info(
            "raw_document_read run_id=%s trace_id=%s bytes=%d duration_ms=%.3f",
            run_id,
            trace_id,
            len(document.body),
            (perf_counter() - started) * 1000,
        )
        return document
