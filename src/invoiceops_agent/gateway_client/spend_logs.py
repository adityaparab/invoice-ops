"""Read bounded, aggregate LiteLLM spend logs with the existing gateway credentials."""

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from time import monotonic

import httpx
from prometheus_client import CollectorRegistry, Gauge
from pydantic import BaseModel, Field, HttpUrl, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from invoiceops_agent.obs.metrics import usd_to_nano_usd

logger = logging.getLogger(__name__)
_PAGE_SIZE = 1000
_MAX_PAGES = 20


class SpendLogSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="LITELLM_",
        env_file=".env",
        env_ignore_empty=True,
        extra="ignore",
        hide_input_in_errors=True,
    )

    api_base: HttpUrl | None = None
    master_key: SecretStr | None = None

    @field_validator("api_base")
    @classmethod
    def validate_api_base(cls, value: HttpUrl | None) -> HttpUrl | None:
        if value is not None and (value.path or "").rstrip("/") != "/v1":
            raise ValueError("LiteLLM API base must end in /v1")
        return value


class SpendLog(BaseModel):
    spend: Decimal = Field(ge=0, allow_inf_nan=False)


class SpendPage(BaseModel):
    data: list[SpendLog]
    page: int = Field(ge=1)
    total_pages: int = Field(ge=0)
    total_is_capped: bool = False


class SpendLogReader:
    def __init__(
        self,
        settings: SpendLogSettings,
        client: httpx.AsyncClient,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._settings = settings
        self._client = client
        self._clock = clock

    async def last_hour_usd(self) -> Decimal:
        if self._settings.api_base is None or self._settings.master_key is None:
            raise ValueError("LiteLLM spend logs are unconfigured")
        now = self._clock().astimezone(UTC)
        start = now - timedelta(hours=1)
        url = str(self._settings.api_base).rstrip("/").removesuffix("/v1") + "/spend/logs/v2"
        total = Decimal(0)
        for page_number in range(1, _MAX_PAGES + 1):
            response = await self._client.get(
                url,
                headers={"Authorization": "Bearer " + self._settings.master_key.get_secret_value()},
                params={
                    "start_date": start.strftime("%Y-%m-%d %H:%M:%S"),
                    "end_date": now.strftime("%Y-%m-%d %H:%M:%S"),
                    "page": page_number,
                    "page_size": _PAGE_SIZE,
                },
            )
            response.raise_for_status()
            page = SpendPage.model_validate_json(response.content)
            if page.page != page_number:
                raise ValueError("LiteLLM spend log page number mismatch")
            total += sum((entry.spend for entry in page.data), Decimal(0))
            if not page.data or (not page.total_is_capped and page_number >= page.total_pages):
                return total
        raise ValueError("LiteLLM spend log window exceeded the bounded page limit")


class SpendLogSampler:
    """Cache one-hour spend snapshots; expose unavailable data explicitly."""

    def __init__(
        self,
        reader: SpendLogReader | None,
        registry: CollectorRegistry,
        *,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._reader = reader
        self._clock = clock
        self._refresh_after = 0.0
        self._lock = asyncio.Lock()
        self._available = Gauge(
            "invoiceops_litellm_spend_logs_available",
            "Whether the LiteLLM spend logs endpoint returned a complete one-hour window.",
            registry=registry,
        )
        self._spend = Gauge(
            "invoiceops_litellm_spend_last_hour_nusd",
            "Nano-USD spend in LiteLLM's most recent complete one-hour log window.",
            ["window"],
            registry=registry,
        )
        self._available.set(0)
        self._spend.clear()

    async def refresh(self) -> None:
        if self._reader is None or self._clock() < self._refresh_after:
            return
        async with self._lock:
            if self._clock() < self._refresh_after:
                return
            try:
                amount = await self._reader.last_hour_usd()
            except (httpx.HTTPError, ValueError) as error:
                self._available.set(0)
                self._spend.clear()
                logger.warning(
                    "event=litellm.spend_logs_unavailable error_type=%s", type(error).__name__
                )
            else:
                self._spend.labels(window="1h").set(usd_to_nano_usd(amount))
                self._available.set(1)
            self._refresh_after = self._clock() + 60
