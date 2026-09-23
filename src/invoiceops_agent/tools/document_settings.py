"""Explicit document byte, decoded-size, and preparation-time allowances."""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class DocumentSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="INVOICEOPS_EXTRACTION_", extra="ignore", hide_input_in_errors=True
    )
    max_document_bytes: int = Field(default=10 * 1024 * 1024, gt=0, le=10 * 1024 * 1024)
    storage_timeout_seconds: float = Field(default=10, gt=0, le=60, allow_inf_nan=False)
    preparation_timeout_seconds: float = Field(default=5, gt=0, le=30, allow_inf_nan=False)
    max_parallel_parses: int = Field(default=2, ge=1, le=4)
    max_image_dimension: int = Field(default=6000, gt=0, le=10_000)
    max_image_pixels: int = Field(default=20_000_000, gt=0, le=40_000_000)
    max_pdf_pages: int = Field(default=4, ge=1, le=10)
    max_pdf_objects: int = Field(default=10_000, ge=1, le=20_000)
    max_pdf_page_points: int = Field(default=2000, gt=0, le=4000)
