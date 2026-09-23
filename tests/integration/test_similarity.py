"""Real pgvector decisions, model isolation, and atomic ledger storage."""

from uuid import UUID

import psycopg
import pytest
from tests.integration.support import INVOICE_ID, RUN_ID
from tests.integration.test_ledger import ledger_runtime_dsn as ledger_runtime_dsn
from tests.integration.test_ledger import runtime_connection, writer
from tests.unit.matching_support import matching_request, snapshot_for

from invoiceops_agent.agents.near_duplicate import EmbeddingContractError, NearDuplicateAgent
from invoiceops_agent.gateway_client.schemas import (
    EmbeddingRequest,
    EmbeddingValue,
    GatewayProvenance,
    GatewayResult,
    TokenUsage,
)
from invoiceops_agent.ledger.connection import LedgerConnection
from invoiceops_agent.ledger.errors import LedgerStorageError
from invoiceops_agent.ledger.reader import LedgerReader
from invoiceops_agent.ledger.schemas import AppendEvent, LedgerEvent
from invoiceops_agent.schemas.similarity import SimilarityRequest, SimilarityResult

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
SECOND_INVOICE = UUID("00000000-0000-4000-8000-000000000010")
SECOND_RUN = UUID("00000000-0000-4000-8000-000000000011")
THIRD_INVOICE = UUID("00000000-0000-4000-8000-000000000012")
THIRD_RUN = UUID("00000000-0000-4000-8000-000000000013")


class FakeEmbeddingGateway:
    def __init__(
        self, vector: tuple[float, ...], model_version: str = "synthetic-embed@v1"
    ) -> None:
        self.vector = vector
        self.model_version = model_version

    async def embed(self, request: EmbeddingRequest) -> GatewayResult[EmbeddingValue]:
        return GatewayResult(
            value=EmbeddingValue(vectors=(self.vector,)),
            provenance=GatewayProvenance(
                alias="embed",
                model=self.model_version,
                model_version=self.model_version,
                prompt_version=request.prompt_version,
            ),
            usage=TokenUsage(input_tokens=10, output_tokens=0, total_tokens=10),
            attempts=1,
            latency_ms=1,
        )


def _seed_second(
    connection: psycopg.Connection[tuple[object, ...]], invoice_id: UUID, run_id: UUID, fill: str
) -> None:
    connection.execute(
        "INSERT INTO invoices (id, content_hash, raw_ref, content_type, source) "
        "VALUES (%s, %s, %s, 'application/pdf', 'UPLOAD')",
        (invoice_id, fill * 64, f"sha256/{fill * 2}/{fill * 64}"),
    )
    connection.execute(
        "INSERT INTO runs (id, invoice_id, graph_version, trace_id) "
        "VALUES (%s, %s, 'graph@v1', %s)",
        (run_id, invoice_id, "0" * 32),
    )


def _request(invoice_id: UUID, run_id: UUID) -> SimilarityRequest:
    return SimilarityRequest(
        run_id=run_id,
        invoice_id=invoice_id,
        trace_id="f" * 32,
        extraction=matching_request(snapshot_for()).extraction,
    )


async def test_pgvector_near_duplicate_and_model_isolation(
    migrated_database: psycopg.Connection[tuple[object, ...]], ledger_runtime_dsn: str
) -> None:
    _seed_second(migrated_database, SECOND_INVOICE, SECOND_RUN, "b")
    _seed_second(migrated_database, THIRD_INVOICE, THIRD_RUN, "c")
    first_vector = (1.0, *(0.0 for _ in range(383)))
    close_vector = (0.99, 0.01, *(0.0 for _ in range(382)))
    first = NearDuplicateAgent(
        FakeEmbeddingGateway(first_vector),
        lambda: runtime_connection(ledger_runtime_dsn),
        writer(),
    )
    assert (await first.detect(_request(INVOICE_ID, RUN_ID))).status == "NO_MATCH"
    second = NearDuplicateAgent(
        FakeEmbeddingGateway(close_vector),
        lambda: runtime_connection(ledger_runtime_dsn),
        writer(),
    )
    decision = await second.detect(_request(SECOND_INVOICE, SECOND_RUN))
    assert decision.status == "NEAR_DUPLICATE"
    assert decision.candidate is not None
    assert decision.candidate.invoice_id == INVOICE_ID
    assert decision.candidate.cosine_similarity > decision.config.minimum_cosine_similarity
    other_model = NearDuplicateAgent(
        FakeEmbeddingGateway(close_vector, "other-embed@v1"),
        lambda: runtime_connection(ledger_runtime_dsn),
        writer(),
    )
    assert (await other_model.detect(_request(THIRD_INVOICE, THIRD_RUN))).status == "NO_MATCH"
    async with runtime_connection(ledger_runtime_dsn) as connection:
        page = await LedgerReader().for_run(connection, SECOND_RUN, trace_id="f" * 32)
        stored = await connection.execute(
            "SELECT embedding IS NOT NULL AS has_embedding, embedding_model_version "
            "FROM invoices WHERE id = %s",
            (SECOND_INVOICE,),
        )
        row = await stored.fetchone()
    assert row == {"has_embedding": True, "embedding_model_version": "synthetic-embed@v1"}
    assert len(page.events) == 1
    assert page.events[0].event_type == "similarity.completed"
    assert page.events[0].versions.model_version == "synthetic-embed@v1"
    assert SimilarityResult.model_validate(page.events[0].payload) == decision


async def test_invalid_embedding_does_not_write_or_audit(
    ledger_runtime_dsn: str,
) -> None:
    agent = NearDuplicateAgent(
        FakeEmbeddingGateway((1.0, 0.0)),
        lambda: runtime_connection(ledger_runtime_dsn),
        writer(),
    )
    with pytest.raises(EmbeddingContractError):
        await agent.detect(_request(INVOICE_ID, RUN_ID))
    async with runtime_connection(ledger_runtime_dsn) as connection:
        row = await (
            await connection.execute("SELECT embedding FROM invoices WHERE id = %s", (INVOICE_ID,))
        ).fetchone()
        page = await LedgerReader().for_run(connection, RUN_ID, trace_id="f" * 32)
    assert row is not None and row["embedding"] is None
    assert page.events == []


class FailingWriter:
    async def append(
        self, connection: LedgerConnection, command: AppendEvent, *, trace_id: str
    ) -> LedgerEvent:
        raise LedgerStorageError(
            "Synthetic audit failure", run_id=command.run_id, trace_id=trace_id
        )


async def test_audit_failure_rolls_back_embedding(
    ledger_runtime_dsn: str,
) -> None:
    vector = (1.0, *(0.0 for _ in range(383)))
    agent = NearDuplicateAgent(
        FakeEmbeddingGateway(vector),
        lambda: runtime_connection(ledger_runtime_dsn),
        FailingWriter(),
    )
    with pytest.raises(LedgerStorageError):
        await agent.detect(_request(INVOICE_ID, RUN_ID))
    async with runtime_connection(ledger_runtime_dsn) as connection:
        row = await (
            await connection.execute(
                "SELECT embedding, embedding_model_version FROM invoices WHERE id = %s",
                (INVOICE_ID,),
            )
        ).fetchone()
    assert row is not None
    assert row["embedding"] is None and row["embedding_model_version"] is None
