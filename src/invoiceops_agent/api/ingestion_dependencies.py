"""Upload resources have explicit ASGI lifespan ownership and injectable seams."""

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass

from invoiceops_agent.api.settings import ApiSettings
from invoiceops_agent.graph.ingestion import IngestionService, UploadService
from invoiceops_agent.tools.ingestion_repository import IngestionRepository
from invoiceops_agent.tools.raw_storage import s3_storage

UploadFactory = Callable[[ApiSettings], AbstractAsyncContextManager[UploadService | None]]


@dataclass
class UploadRuntime:
    service: UploadService | None = None


@asynccontextmanager
async def default_upload_factory(settings: ApiSettings) -> AsyncIterator[UploadService | None]:
    if (
        settings.postgres_dsn is None
        or settings.minio_url is None
        or settings.minio_access_key is None
        or settings.minio_secret_key is None
        or settings.service_token is None
    ):
        yield None
        return
    async with s3_storage(
        endpoint=str(settings.minio_url),
        access_key=settings.minio_access_key.get_secret_value(),
        secret_key=settings.minio_secret_key.get_secret_value(),
        bucket=settings.raw_bucket,
        timeout_seconds=settings.storage_timeout_seconds,
    ) as storage:
        yield IngestionService(
            IngestionRepository(settings.postgres_dsn.get_secret_value()),
            storage,
        )
