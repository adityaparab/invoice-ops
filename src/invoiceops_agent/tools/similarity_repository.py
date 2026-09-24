"""Search and persist pgvector embeddings in one caller-owned transaction."""

import asyncio
import logging
from time import perf_counter
from uuid import UUID

import psycopg
from psycopg.rows import DictRow

from invoiceops_agent.schemas.similarity import (
    EmbeddingVector,
    SimilarityCandidate,
    SimilarityConfig,
)
from invoiceops_agent.tools.similarity import choose_candidate

logger = logging.getLogger(__name__)


class SimilarityRepositoryError(Exception):
    """A similarity decision could not be persisted."""


class InvoiceNotFound(SimilarityRepositoryError):
    """The run references an invoice absent from the runtime database."""


def _vector_literal(vector: EmbeddingVector) -> str:
    return "[" + ",".join(repr(value) for value in vector.values) + "]"


class SimilarityRepository:
    @staticmethod
    async def detect_and_store(
        connection: psycopg.AsyncConnection[DictRow],
        invoice_id: UUID,
        vector: EmbeddingVector,
        model_version: str,
        config: SimilarityConfig,
        *,
        invoice_number: str | None,
        po_number: str | None,
    ) -> SimilarityCandidate | None:
        """Compare only matching invoice and PO identities under one model lock."""
        started = perf_counter()
        literal = _vector_literal(vector)
        invoice_identity = (
            invoice_number.strip() if invoice_number and invoice_number.strip() else None
        )
        po_identity = po_number.strip() if po_number and po_number.strip() else None
        try:
            async with asyncio.timeout(10):
                await connection.execute(
                    "SELECT pg_advisory_xact_lock(761923, hashtext(%s))", (model_version,)
                )
                cursor = await connection.execute(
                    "WITH comparable AS MATERIALIZED ("
                    "SELECT id, embedding <=> %s::vector AS distance "
                    "FROM public.invoices WHERE id <> %s AND embedding IS NOT NULL "
                    "AND embedding_model_version = %s "
                    "AND invoice_number = %s AND po_number = %s) "
                    "SELECT id, distance FROM comparable ORDER BY distance, id LIMIT 1",
                    (literal, invoice_id, model_version, invoice_identity, po_identity),
                )
                row = await cursor.fetchone()
                candidate = (
                    choose_candidate(row["id"], row["distance"], config)
                    if row is not None
                    else None
                )
                updated = await connection.execute(
                    "UPDATE public.invoices SET embedding = %s::vector, "
                    "embedding_model_version = %s, invoice_number = %s, po_number = %s "
                    "WHERE id = %s RETURNING id",
                    (literal, model_version, invoice_identity, po_identity, invoice_id),
                )
                if await updated.fetchone() is None:
                    raise InvoiceNotFound("Invoice for similarity decision is missing")
        except (TimeoutError, psycopg.Error) as error:
            logger.error(
                "similarity_storage_failed invoice_id=%s error_type=%s duration_ms=%.3f",
                invoice_id,
                type(error).__name__,
                (perf_counter() - started) * 1000,
            )
            raise SimilarityRepositoryError("Similarity storage failed") from None
        logger.info(
            "similarity_stored invoice_id=%s candidate_found=%s duration_ms=%.3f",
            invoice_id,
            candidate is not None,
            (perf_counter() - started) * 1000,
        )
        return candidate
