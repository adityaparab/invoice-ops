"""Exercise the real storage capabilities required by the platform."""

from io import BytesIO

import psycopg
import pytest
from minio import Minio

pytestmark = pytest.mark.integration


def test_postgres_supports_vector_nearest_neighbor_query(
    postgres_connection: psycopg.Connection[tuple[object, ...]],
) -> None:
    postgres_connection.execute("CREATE EXTENSION vector")
    postgres_connection.execute("CREATE TABLE embeddings (id integer PRIMARY KEY, value vector(3))")
    postgres_connection.execute(
        "INSERT INTO embeddings (id, value) VALUES (%s, %s), (%s, %s)",
        (1, "[1, 2, 3]", 2, "[4, 5, 6]"),
    )

    nearest = postgres_connection.execute(
        "SELECT id FROM embeddings ORDER BY value <-> %s::vector LIMIT 1", ("[1, 2, 3]",)
    ).fetchone()
    zero_distance = postgres_connection.execute(
        "SELECT value <-> %s::vector = 0 FROM embeddings WHERE id = %s", ("[1, 2, 3]", 1)
    ).fetchone()

    assert nearest == (1,)
    assert zero_distance == (True,)


def test_minio_preserves_uploaded_object_bytes_and_metadata(minio_client: Minio) -> None:
    bucket = "synthetic-invoices"
    key = "fixtures/invoice-001.txt"
    payload = b"Synthetic invoice fixture\nAmount: 123.45 USD\n"
    minio_client.make_bucket(bucket)
    minio_client.put_object(bucket, key, BytesIO(payload), len(payload), content_type="text/plain")

    metadata = minio_client.stat_object(bucket, key)
    response = minio_client.get_object(bucket, key)
    try:
        retrieved = response.read()
    finally:
        response.close()
        response.release_conn()

    assert retrieved == payload
    assert metadata.size == len(payload)
    assert metadata.content_type == "text/plain"
