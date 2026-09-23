"""Real MinIO reads and committed restricted-role audit events with offline model cassettes."""

from functools import partial

import pytest
from tests.integration.conftest import TEST_PASSWORD, TEST_USER
from tests.integration.support import INVOICE_ID, RUN_ID
from tests.integration.test_ledger import ledger_runtime_dsn, runtime_connection, writer
from tests.unit.extraction_support import extraction_request, raw_document
from tests.unit.test_extraction_agent import CASSETTES, gateway_settings

from invoiceops_agent.agents.extraction import ExtractionAgent
from invoiceops_agent.gateway_client import GatewayClient
from invoiceops_agent.gateway_client.cassettes import CassetteTransport
from invoiceops_agent.ledger.audit import TransactionalAuditSink
from invoiceops_agent.ledger.reader import LedgerReader
from invoiceops_agent.tools.document_preflight import DocumentPreflight
from invoiceops_agent.tools.document_settings import DocumentSettings
from invoiceops_agent.tools.raw_document_reader import S3DocumentReader
from invoiceops_agent.tools.raw_storage import s3_storage

__all__ = ["ledger_runtime_dsn"]
pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.mark.parametrize(
    "scenario,status,event_type",
    [
        ("extract_success", "EXTRACTED", "extraction.completed"),
        ("extract_malformed", "ESCALATED", "extraction.escalated"),
    ],
)
async def test_real_raw_read_commits_agent_outcome_with_source_and_version_pins(
    ledger_runtime_dsn: str,
    minio_endpoint: str,
    scenario: str,
    status: str,
    event_type: str,
) -> None:
    settings = DocumentSettings()
    document = raw_document()
    request = extraction_request(scenario=scenario).model_copy(
        update={"run_id": RUN_ID, "invoice_id": INVOICE_ID}
    )
    async with s3_storage(
        endpoint=minio_endpoint,
        access_key=TEST_USER,
        secret_key=TEST_PASSWORD,
        bucket="invoiceops-raw",
        timeout_seconds=5,
    ) as storage:
        await storage.client.create_bucket(Bucket="invoiceops-raw")
        assert await storage.put(document, trace_id=request.trace_id) == request.raw_ref
        async with runtime_connection(ledger_runtime_dsn) as connection:
            await connection.execute(
                "UPDATE public.invoices SET raw_ref=%s, content_hash=%s, content_type=%s "
                "WHERE id=%s",
                (request.raw_ref, request.content_hash, request.content_type, INVOICE_ID),
            )
        async with GatewayClient(
            gateway_settings(), transport=CassetteTransport(CASSETTES)
        ) as gateway:
            extractor = ExtractionAgent(
                S3DocumentReader(storage.client, storage.bucket, settings),
                DocumentPreflight(settings),
                gateway,
                TransactionalAuditSink(partial(runtime_connection, ledger_runtime_dsn), writer()),
            )
            result = await extractor.extract(request)
    assert result.status == status
    async with runtime_connection(ledger_runtime_dsn) as connection:
        history = await LedgerReader().for_run(connection, RUN_ID, trace_id=request.trace_id)
    assert len(history.events) == 1
    event = history.events[0]
    assert event.event_type == event_type and event.actor_type == "AGENT"
    assert event.versions.model_version == "synthetic-extraction@v1"
    assert event.versions.prompt_version == result.calls[-1].prompt_version
    assert event.payload["content_hash"] == document.content_hash
    assert event.payload["result"] == result.model_dump(mode="json")
