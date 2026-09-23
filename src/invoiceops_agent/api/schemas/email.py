"""Minimal signed email stub: one attachment and no personal email metadata."""

from pydantic import BaseModel, ConfigDict, Field

from invoiceops_agent.tools.ingestion_schemas import DocumentType


class EmailAttachment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    content_type: DocumentType
    content_base64: str = Field(min_length=1, repr=False)


class EmailWebhookRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    attachment: EmailAttachment


def email_request_schema() -> dict[str, object]:
    """Inline the single child schema because authentication requires manual body parsing."""
    schema = EmailWebhookRequest.model_json_schema()
    attachment = schema.pop("$defs")["EmailAttachment"]
    schema["properties"]["attachment"] = attachment
    return schema
