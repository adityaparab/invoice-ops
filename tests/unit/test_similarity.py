"""Offline vector bounds, deterministic summary, threshold, and LiteLLM settings."""

from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import ValidationError
from tests.unit.matching_support import matching_request, snapshot_for

from invoiceops_agent.agents.near_duplicate_settings import LiteLLMEmbeddingSettings
from invoiceops_agent.schemas.similarity import (
    EMBEDDING_DIMENSIONS,
    EmbeddingVector,
    SimilarityConfig,
)
from invoiceops_agent.tools.similarity import choose_candidate, invoice_summary

pytestmark = pytest.mark.unit


def test_summary_is_stable_and_omits_bank_account() -> None:
    extraction = matching_request(snapshot_for()).extraction
    first = invoice_summary(extraction)
    assert first == invoice_summary(extraction)
    assert extraction.invoice_number.value is not None
    assert extraction.bank_account_iban.value is not None
    assert extraction.vendor_tax_id.value is not None
    assert extraction.invoice_number.value in first
    assert extraction.bank_account_iban.value not in first
    assert extraction.vendor_tax_id.value not in first


def test_vector_requires_finite_nonzero_384_dimensions() -> None:
    values = (1.0, *(0.0 for _ in range(EMBEDDING_DIMENSIONS - 1)))
    assert EmbeddingVector(values=values).values == values
    for invalid in (values[:-1], (0.0,) * EMBEDDING_DIMENSIONS, (float("nan"), *values[1:])):
        with pytest.raises(ValidationError):
            EmbeddingVector(values=invalid)


def test_similarity_threshold_is_inclusive_and_exact_decimal() -> None:
    candidate_id = UUID(int=99)
    config = SimilarityConfig(minimum_cosine_similarity=Decimal("0.95"))
    boundary = choose_candidate(candidate_id, 0.05, config)
    assert boundary is not None
    assert boundary.cosine_similarity == Decimal("0.95")
    assert choose_candidate(candidate_id, 0.05001, config) is None
    with pytest.raises(ValueError, match="finite"):
        choose_candidate(candidate_id, float("nan"), config)
    with pytest.raises(ValidationError):
        SimilarityConfig.model_validate({"minimum_cosine_similarity": 0.95})


def test_embedding_gateway_uses_only_litellm_url_key_and_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LITELLM_API_BASE", "https://gateway.example.test/v1")
    monkeypatch.setenv("LITELLM_MASTER_KEY", "synthetic-key")
    monkeypatch.setenv("LITELLM_EMBED_MODEL", "synthetic-384")
    monkeypatch.setenv("LITELLM_CONFIG", "not-a-real-config.yaml")
    settings = LiteLLMEmbeddingSettings(_env_file=None)
    gateway = settings.gateway_settings()
    assert str(gateway.base_url) == "https://gateway.example.test/v1"
    assert gateway.api_key.get_secret_value() == "synthetic-key"
    assert gateway.aliases["embed"].model_name == "synthetic-384"
    assert set(gateway.aliases) == {"embed"}
    monkeypatch.setenv("LITELLM_EMBED_MODEL", "__legacy_unpinned__")
    with pytest.raises(ValidationError, match="Legacy vectors"):
        LiteLLMEmbeddingSettings(_env_file=None)
