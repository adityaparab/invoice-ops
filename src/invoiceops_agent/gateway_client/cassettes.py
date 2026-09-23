"""Explicit HTTP record/replay transport; replay never has a network fallback."""

import asyncio
import hashlib
import json
import re
from pathlib import Path
from typing import Literal

import httpx2
from pydantic import Field, JsonValue

from invoiceops_agent.gateway_client.schemas import Contract, ModelAlias


class CassetteMismatch(Exception):
    """A missing or different immutable scenario; deliberately contains no request data."""


class Cassette(Contract):
    alias: ModelAlias
    scenario: str
    prompt_version: str
    request_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    status_code: int = Field(ge=100, le=599)
    headers: dict[str, str]
    response: JsonValue


def _identity(request: httpx2.Request) -> tuple[str, str, str, str]:
    try:
        body = json.loads(request.content)
        alias = body["model"]
        scenario = request.headers["X-InvoiceOps-Scenario"]
        version = request.headers["X-InvoiceOps-Prompt-Version"]
        if alias not in {
            "extract-vision",
            "triage-reasoner",
            "eval-judge",
            "embed",
        } or not re.fullmatch(r"[a-zA-Z0-9_\-]{1,80}", scenario):
            raise ValueError("Invalid cassette identity")
        payload = {
            "method": request.method,
            "path": request.url.path,
            "body": body,
            "schema": request.headers.get("X-InvoiceOps-Schema-Hash"),
        }
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
                "utf-8"
            )
        ).hexdigest()
        return alias, scenario, version, digest
    except (KeyError, ValueError, TypeError):
        raise CassetteMismatch("Invalid cassette identity") from None


class CassetteTransport(httpx2.AsyncBaseTransport):
    def __init__(
        self,
        directory: Path,
        *,
        mode: Literal["replay", "record"] = "replay",
        upstream: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        if mode == "record" and upstream is None:
            raise ValueError("Recording requires an explicitly supplied upstream transport")
        if mode == "replay" and upstream is not None:
            raise ValueError("Replay cannot have an upstream transport")
        self._directory = directory
        self._mode = mode
        self._upstream = upstream

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        alias, scenario, version, digest = _identity(request)
        version_hash = hashlib.sha256(version.encode("utf-8")).hexdigest()[:16]
        path = self._directory / f"{alias}__{scenario}__{version_hash}.json"
        if self._mode == "replay":
            cassette = await asyncio.to_thread(self._read, path)
            if (
                cassette.alias,
                cassette.scenario,
                cassette.prompt_version,
                cassette.request_sha256,
            ) != (alias, scenario, version, digest):
                raise CassetteMismatch("Cassette request or schema changed")
            return httpx2.Response(
                cassette.status_code,
                headers=cassette.headers,
                json=cassette.response,
                request=request,
            )
        if await asyncio.to_thread(path.exists):
            raise CassetteMismatch("An existing cassette cannot be overwritten")
        assert self._upstream is not None
        response = await self._upstream.handle_async_request(request)
        await response.aread()
        try:
            cassette = Cassette.model_validate(
                {
                    "alias": alias,
                    "scenario": scenario,
                    "prompt_version": version,
                    "request_sha256": digest,
                    "status_code": response.status_code,
                    "headers": {
                        key: value
                        for key, value in response.headers.items()
                        if key.lower()
                        in {
                            "content-type",
                            "retry-after",
                            "x-litellm-response-cost",
                        }
                    },
                    "response": response.json(),
                }
            )
            await asyncio.to_thread(self._write, path, cassette)
        except (ValueError, OSError):
            await response.aclose()
            raise CassetteMismatch("Cassette could not be recorded") from None
        return response

    @staticmethod
    def _read(path: Path) -> Cassette:
        try:
            return Cassette.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise CassetteMismatch("Cassette is missing or invalid") from None

    @staticmethod
    def _write(path: Path, cassette: Cassette) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as stream:
            stream.write(cassette.model_dump_json(indent=2) + "\n")

    async def aclose(self) -> None:
        if self._upstream is not None:
            await self._upstream.aclose()
