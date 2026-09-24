"""Immutable logical-call HTTP sequences; replay never has a network fallback."""

import asyncio
import hashlib
import json
import re
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Literal

import httpx2
from pydantic import Field, JsonValue, TypeAdapter

from invoiceops_agent.artifacts import write_new_artifact
from invoiceops_agent.gateway_client.schemas import Contract, ModelAlias


class CassetteMismatch(Exception):
    """A missing or different immutable scenario; deliberately contains no request data."""


class CassetteIdentity(Contract):
    alias: ModelAlias
    scenario: str
    prompt_version: str
    request_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class Cassette(CassetteIdentity):
    """Legacy single-response fixture; committed files remain unchanged."""

    status_code: int = Field(ge=100, le=599)
    headers: dict[str, str]
    response: JsonValue


class RecordedResponse(Contract):
    kind: Literal["response"] = "response"
    status_code: int = Field(ge=100, le=599)
    headers: dict[str, str]
    response: JsonValue


class RecordedFailure(Contract):
    kind: Literal["connection_error", "timeout"]


Outcome = Annotated[RecordedResponse | RecordedFailure, Field(discriminator="kind")]


class CassetteSequence(CassetteIdentity):
    format_version: Literal[2] = 2
    outcomes: tuple[Outcome, ...] = Field(min_length=1, max_length=6)


def _identity(request: httpx2.Request) -> CassetteIdentity:
    try:
        body = json.loads(request.content)
        scenario = request.headers["X-InvoiceOps-Scenario"]
        if not re.fullmatch(r"[a-zA-Z0-9_\-]{1,80}", scenario):
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
        return CassetteIdentity.model_validate(
            {
                "alias": body["model"],
                "scenario": scenario,
                "prompt_version": request.headers["X-InvoiceOps-Prompt-Version"],
                "request_sha256": digest,
            }
        )
    except (KeyError, ValueError, TypeError):
        raise CassetteMismatch("Invalid cassette identity") from None


async def _disk[T](operation: Callable[[], T]) -> T:
    # Keep reservations until an in-flight local disk operation has actually settled.
    task = asyncio.create_task(asyncio.to_thread(operation))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        except (OSError, CassetteMismatch):
            pass
        raise


@dataclass
class _Invocation:
    identity: CassetteIdentity | None = None
    path: Path | None = None
    reserved: bool = False
    recordable: bool = True
    outcomes: list[RecordedResponse | RecordedFailure] = field(default_factory=list)
    replay: tuple[Outcome, ...] = ()
    index: int = 0


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
        self._current: ContextVar[_Invocation | None] = ContextVar(
            "cassette_invocation", default=None
        )
        self._active = 0
        self._closed = False

    @asynccontextmanager
    async def invocation(self) -> AsyncIterator[None]:
        """Scope one gateway call, including all attempts and terminal validation."""
        if self._closed:
            raise CassetteMismatch("Cassette transport is closed")
        invocation = _Invocation()
        token = self._current.set(invocation)
        self._active += 1
        try:
            try:
                yield
            except (asyncio.CancelledError, TimeoutError):
                raise
            except Exception:
                await self._finish(invocation)
                raise
            else:
                await self._finish(invocation)
        finally:
            self._current.reset(token)
            self._active -= 1
            if invocation.reserved and invocation.path is not None:
                path = invocation.path
                try:
                    await _disk(lambda: path.with_suffix(".recording").unlink())
                except OSError:
                    raise CassetteMismatch("Cassette reservation could not be released") from None

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        invocation = self._current.get()
        if invocation is None:
            raise CassetteMismatch("Cassette transport requires a gateway invocation")
        try:
            identity = _identity(request)
            if invocation.identity is None:
                await self._begin(invocation, identity)
            elif invocation.identity != identity:
                raise CassetteMismatch("Cassette request or schema changed during retries")
            if self._mode == "replay":
                return self._replay(invocation, request)
            return await self._record(invocation, request)
        except (CassetteMismatch, asyncio.CancelledError):
            invocation.recordable = False
            raise

    async def _begin(self, invocation: _Invocation, identity: CassetteIdentity) -> None:
        version_hash = hashlib.sha256(identity.prompt_version.encode("utf-8")).hexdigest()[:16]
        path = self._directory / f"{identity.alias}__{identity.scenario}__{version_hash}.json"
        invocation.identity = identity
        invocation.path = path
        if self._mode == "record":

            def reserve() -> None:
                self._reserve(path)
                invocation.reserved = True

            await _disk(reserve)
        else:
            cassette = await _disk(lambda: self._read(path))
            if (
                CassetteIdentity.model_validate(
                    cassette.model_dump(
                        include={"alias", "scenario", "prompt_version", "request_sha256"}
                    )
                )
                != identity
            ):
                raise CassetteMismatch("Cassette request or schema changed")
            invocation.replay = cassette.outcomes

    @staticmethod
    def _replay(invocation: _Invocation, request: httpx2.Request) -> httpx2.Response:
        if invocation.index >= len(invocation.replay):
            raise CassetteMismatch("Cassette retry sequence is exhausted")
        outcome = invocation.replay[invocation.index]
        invocation.index += 1
        if isinstance(outcome, RecordedFailure):
            if outcome.kind == "timeout":
                raise httpx2.ReadTimeout("Recorded timeout", request=request)
            raise httpx2.ConnectError("Recorded connection failure", request=request)
        return httpx2.Response(
            outcome.status_code, headers=outcome.headers, json=outcome.response, request=request
        )

    async def _record(self, invocation: _Invocation, request: httpx2.Request) -> httpx2.Response:
        assert self._upstream is not None
        try:
            response = await self._upstream.handle_async_request(request)
            try:
                await response.aread()
                outcome = RecordedResponse.model_validate(
                    {
                        "status_code": response.status_code,
                        "headers": {
                            key: value
                            for key, value in response.headers.items()
                            if key.lower()
                            in {"content-type", "retry-after", "x-litellm-response-cost"}
                        },
                        "response": response.json(),
                    }
                )
            finally:
                await response.aclose()
        except httpx2.TimeoutException:
            invocation.outcomes.append(RecordedFailure(kind="timeout"))
            raise
        except httpx2.TransportError:
            invocation.outcomes.append(RecordedFailure(kind="connection_error"))
            raise
        except ValueError:
            raise CassetteMismatch("Cassette response is not valid JSON") from None
        invocation.outcomes.append(outcome)
        return response

    async def _finish(self, invocation: _Invocation) -> None:
        if not invocation.recordable or invocation.identity is None:
            return
        if self._mode == "replay":
            if invocation.index != len(invocation.replay):
                raise CassetteMismatch("Cassette retry sequence was not fully consumed")
            return
        if not invocation.outcomes:
            return
        assert invocation.path is not None
        path = invocation.path
        try:
            sequence = CassetteSequence.model_validate(
                {**invocation.identity.model_dump(), "outcomes": invocation.outcomes}
            )
            await _disk(lambda: self._write(path, sequence))
        except (ValueError, OSError):
            raise CassetteMismatch("Cassette could not be recorded") from None

    @staticmethod
    def _reserve(path: Path) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                raise CassetteMismatch("An existing cassette cannot be overwritten")
            reservation = path.with_suffix(".recording")
            with reservation.open("x"):
                pass
            if path.exists():
                reservation.unlink()
                raise CassetteMismatch("An existing cassette cannot be overwritten")
        except OSError:
            raise CassetteMismatch("Cassette is already recording or unavailable") from None

    @staticmethod
    def _read(path: Path) -> CassetteSequence:
        try:
            cassette: CassetteSequence | Cassette = TypeAdapter(
                CassetteSequence | Cassette
            ).validate_json(path.read_text(encoding="utf-8"))
            if isinstance(cassette, CassetteSequence):
                return cassette
            return CassetteSequence.model_validate(
                {
                    **cassette.model_dump(exclude={"status_code", "headers", "response"}),
                    "outcomes": [
                        {
                            "kind": "response",
                            "status_code": cassette.status_code,
                            "headers": cassette.headers,
                            "response": cassette.response,
                        }
                    ],
                }
            )
        except (OSError, ValueError):
            raise CassetteMismatch("Cassette is missing or invalid") from None

    @staticmethod
    def _write(path: Path, cassette: CassetteSequence) -> None:
        write_new_artifact(path, (cassette.model_dump_json(indent=2) + "\n").encode("utf-8"))

    async def aclose(self) -> None:
        if self._active:
            raise CassetteMismatch("Cannot close a cassette transport with active calls")
        if self._closed:
            return
        self._closed = True
        if self._upstream is not None:
            await self._upstream.aclose()


class AliasCassetteTransport(CassetteTransport):
    """Replay by task alias while preserving the caller's configured model provenance."""

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        try:
            alias: ModelAlias = TypeAdapter(ModelAlias).validate_python(
                request.headers["X-InvoiceOps-Alias"]
            )
            body = json.loads(request.content)
            model_name = body["model"]
            if not isinstance(model_name, str):
                raise ValueError("Invalid model name")
            body["model"] = alias
        except (KeyError, ValueError, TypeError):
            raise CassetteMismatch("Invalid alias cassette request") from None
        normalized = httpx2.Request(
            request.method,
            request.url,
            headers=request.headers,
            content=json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode(),
        )
        response = await super().handle_async_request(normalized)
        payload = response.json()
        if isinstance(payload, dict) and isinstance(payload.get("model"), str):
            payload["model"] = model_name
        return httpx2.Response(
            response.status_code, headers=response.headers, json=payload, request=request
        )
