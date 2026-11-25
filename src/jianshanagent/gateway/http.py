"""Server-to-server chat over HTTP. Terminate TLS at a trusted reverse proxy.

The bearer token grants its app the right to assert any user_id in that app.
Keep tokens on trusted servers; browsers should authenticate to their own backend.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import logging
import math
import re
import struct
import uuid
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from aiohttp import web
from aiohttp.web_protocol import RequestHandler, RequestPayloadError
from PIL import Image

from jianshanagent.core.messages import (
    CHANNEL_HTTP,
    ChatScope,
    ImageRef,
    InboundMessage,
    OutboundMessage,
)
from jianshanagent.core.usage import usage_scope
from jianshanagent.gateway.base import GatewayContext

_LOG = logging.getLogger(__name__)
# Parser and access logs can contain attacker-controlled headers or paths.
_PROTOCOL_LOG = logging.Logger(__name__ + ".protocol")
_PROTOCOL_LOG.disabled = True

_APP_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
_TOKEN = re.compile(r"[A-Za-z0-9_-]{32,256}")
_DATA_PREFIXES = {
    "data:image/png;base64": ("image/png", "PNG"),
    "data:image/jpeg;base64": ("image/jpeg", "JPEG"),
}
_MAX_TEXT = 4_000
_MAX_IMAGES = 4
_MAX_IMAGE_BYTES = 2 * 1024 * 1024
_MAX_TOTAL_IMAGE_BYTES = 4 * 1024 * 1024
_MAX_BODY = 4 * ((_MAX_TOTAL_IMAGE_BYTES + 2) // 3) + 32 * 1024
_MAX_PIXELS = 4_194_304
_MAX_DIMENSION = 4096
_MAX_PROGRESS = 32
_MAX_FRAME = 512 * 1024
_HEADER_TIMEOUT = 5.0
_BODY_TIMEOUT = 10.0
_WRITE_TIMEOUT = 5.0
_RATE_WINDOW = 60.0


def _integer(settings: Mapping[str, Any], key: str, default: int, low: int, high: int) -> int:
    value = settings.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"HTTP {key} must be an integer between {low} and {high}")
    return value


def _clients(secrets: Mapping[str, Any], *, allow_empty: bool = False) -> dict[bytes, str]:
    clients = secrets.get("clients", {})
    if not isinstance(clients, Mapping) or len(clients) > 256 or (not clients and not allow_empty):
        raise ValueError("HTTP secrets.clients must be an app-to-token mapping (1–256 apps)")
    result: dict[bytes, str] = {}
    for app_id, token in clients.items():
        if not isinstance(app_id, str) or not _APP_ID.fullmatch(app_id):
            raise ValueError("Invalid HTTP client app ID")
        if not isinstance(token, str) or not _TOKEN.fullmatch(token):
            raise ValueError("HTTP client tokens must be 32–256 base64url characters")
        digest = hashlib.sha256(token.encode("ascii")).digest()
        if digest in result:
            raise ValueError("HTTP client tokens must be unique across apps")
        result[digest] = app_id
    return result


def _error(status: int, code: str, headers: Mapping[str, str] | None = None) -> web.Response:
    response = web.json_response({"error": code}, status=status, headers=headers)
    response.headers["Cache-Control"] = "no-store"
    response.force_close()
    return response


async def _reject_expect(_: web.Request) -> web.StreamResponse:
    # Never send an unauthenticated automatic 100 Continue.
    return _error(417, "expectation_not_supported")


def _json_object(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _identity(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 128
        or not value.isprintable()
        or value != value.strip()
    ):
        raise ValueError("invalid identity")
    return value


def _digest(*parts: str | None) -> str:
    return hashlib.sha256(json.dumps(parts, separators=(",", ":")).encode("ascii")).hexdigest()


def _check_png(data: bytes) -> None:
    # Pillow reads compressed text/profile chunks while opening a PNG, before
    # the pixel budget can be checked. Also reject animation and trailing data.
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("invalid PNG")
    offset = 8
    for _ in range(1024):
        if offset + 12 > len(data):
            break
        size = int.from_bytes(data[offset : offset + 4], "big")
        chunk = data[offset + 4 : offset + 8]
        offset += size + 12
        if offset > len(data) or chunk in {b"iCCP", b"zTXt", b"iTXt", b"acTL", b"fcTL", b"fdAT"}:
            break
        if chunk == b"IEND" and size == 0 and offset == len(data):
            return
    raise ValueError("unsupported PNG chunks")


def _images(value: Any) -> list[ImageRef]:
    if not isinstance(value, list) or len(value) > _MAX_IMAGES:
        raise ValueError("invalid image list")
    result = []
    remaining = _MAX_TOTAL_IMAGE_BYTES
    for uri in value:
        if not isinstance(uri, str):
            raise ValueError("invalid image")
        prefix, sep, encoded = uri.partition(",")
        cap = min(remaining, _MAX_IMAGE_BYTES)
        if not sep or prefix not in _DATA_PREFIXES or not 1 <= len(encoded) <= 4 * ((cap + 2) // 3):
            raise ValueError("invalid data URI")
        content_type, format_name = _DATA_PREFIXES[prefix]
        data = base64.b64decode(encoded, validate=True)
        if not 1 <= len(data) <= cap:
            raise ValueError("image too large")
        remaining -= len(data)
        if format_name == "PNG":
            _check_png(data)
        with Image.open(io.BytesIO(data), formats=[format_name]) as image:
            width, height = image.size
            if (
                image.format != format_name
                or not 1 <= width <= _MAX_DIMENSION
                or not 1 <= height <= _MAX_DIMENSION
                or width * height > _MAX_PIXELS
            ):
                raise ValueError("unsupported image dimensions or format")
            image.load()
        result.append(
            ImageRef(
                url="",
                content_type=content_type,
                width=width,
                height=height,
                size=len(data),
                data=data,
            )
        )
    return result


def _inbound(body: Any, app_id: str) -> InboundMessage:
    if not isinstance(body, dict) or body.keys() - {"user_id", "conversation_id", "text", "images"}:
        raise ValueError("invalid chat body")
    user = _identity(body.get("user_id"))
    conversation = _identity(body["conversation_id"]) if "conversation_id" in body else None
    text = body.get("text", "")
    if not isinstance(text, str) or len(text) > _MAX_TEXT or "\x00" in text:
        raise ValueError("invalid text")
    text.encode("utf-8")  # Reject lone surrogates from JSON string escapes.
    text = text.strip()
    images = _images(body.get("images", []))
    if not text and not images:
        raise ValueError("empty chat body")
    user_id = f"http-user-{_digest(app_id, user)}"
    # Match the existing eraser's user_suffix for all conversations of this app/user.
    chat_id = f"http-chat-{_digest(app_id, user, conversation)}:user:{user_id}"
    return InboundMessage(
        message_id=f"http-{uuid.uuid4().hex}",
        chat_scope=ChatScope.C2C,
        chat_id=chat_id,
        user_id=user_id,
        content=text,
        images=images,
        channel=CHANNEL_HTTP,
        timestamp=datetime.now(UTC).isoformat(),
    )


def _validate(body: bytearray, app_id: str) -> InboundMessage | None:
    try:
        return _inbound(json.loads(body.decode("utf-8"), object_pairs_hook=_json_object), app_id)
    except (
        OSError,
        ValueError,
        SyntaxError,
        RecursionError,
        struct.error,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ):
        return None


class _RequestHandler(RequestHandler):
    def handle_error(
        self,
        request: web.BaseRequest,
        status: int = 500,
        exc: BaseException | None = None,
        message: str | None = None,
    ) -> web.StreamResponse:
        # The default parser error can contain a fragment of the incoming request.
        return super().handle_error(request, status, message="invalid HTTP request")


class _Connection(asyncio.Protocol):
    """Bound live sockets and the time before aiohttp dispatches request headers."""

    def __init__(
        self,
        factory: Callable[[], asyncio.Protocol],
        connections: dict[asyncio.Transport, asyncio.TimerHandle],
        limit: int,
    ) -> None:
        self._factory = factory
        self._connections = connections
        self._limit = limit
        self._protocol: asyncio.Protocol | None = None
        self._transport: asyncio.Transport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        assert isinstance(transport, asyncio.Transport)
        if len(self._connections) >= self._limit:
            transport.abort()
            return
        self._transport = transport
        self._connections[transport] = asyncio.get_running_loop().call_later(
            _HEADER_TIMEOUT, transport.abort
        )
        self._protocol = self._factory()
        self._protocol.connection_made(transport)

    def connection_lost(self, exc: Exception | None) -> None:
        if self._transport is not None:
            self._connections.pop(self._transport).cancel()
        if self._protocol is not None:
            self._protocol.connection_lost(exc)

    def data_received(self, data: bytes) -> None:
        if self._protocol is not None:
            self._protocol.data_received(data)

    def eof_received(self) -> bool | None:
        if self._protocol is not None:
            return self._protocol.eof_received()
        return None

    def pause_writing(self) -> None:
        if self._protocol is not None:
            self._protocol.pause_writing()

    def resume_writing(self) -> None:
        if self._protocol is not None:
            self._protocol.resume_writing()


class _StreamClosed(Exception):
    pass


def _event(event: str, data: dict[str, Any]) -> bytes:
    payload = json.dumps(data, separators=(",", ":"))
    return f"event: {event}\ndata: {payload}\n\n".encode("ascii")


async def _write(response: web.StreamResponse, frame: bytes) -> None:
    try:
        async with asyncio.timeout(_WRITE_TIMEOUT):
            await response.write(frame)
    except (ConnectionError, RuntimeError, TimeoutError):
        raise _StreamClosed from None


class HttpGateway:
    def __init__(self, context: GatewayContext) -> None:
        settings = context.settings
        host = settings.get("host", "127.0.0.1")
        if (
            not isinstance(host, str)
            or not host
            or len(host) > 253
            or not host.isascii()
            or re.search(r"[^A-Za-z0-9_.:%-]", host)
        ):
            raise ValueError("HTTP host must be a hostname or IP address")
        self._host = host
        self._port = _integer(settings, "port", 18081, 0, 65535)
        self._rate = _integer(settings, "requests_per_minute", 30, 1, 6000)
        self._max_per_app = _integer(settings, "max_inflight_per_app", 2, 1, 64)
        self._max_inflight = _integer(settings, "max_inflight", 8, 1, 64)
        timeout = settings.get("timeout_seconds", 300)
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not 0.1 <= timeout <= 900
        ):
            raise ValueError("HTTP timeout_seconds must be a number between 0.1 and 900")
        self._timeout = float(timeout)
        self._store = context.integrations
        self._clients = _clients(context.secrets, allow_empty=self._store is not None)
        self._handle = context.handle
        self._inflight = 0
        self._app_inflight: dict[str, int] = {}
        self._users_inflight: set[str] = set()
        self._max_validating = min(4, self._max_inflight)
        self._validating: set[asyncio.Task[InboundMessage | None]] = set()
        self._recent: dict[str, deque[float]] = {}
        self._connections: dict[asyncio.Transport, asyncio.TimerHandle] = {}
        self._lifecycle = asyncio.Lock()
        self._runner: web.AppRunner | None = None
        self._listener: asyncio.Server | None = None

    @property
    def legacy_app_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._clients.values()))

    def health(self) -> tuple[bool, str]:
        if self._listener is not None and self._listener.is_serving():
            return True, "listening"
        return False, "not listening"

    async def start(self) -> None:
        async with self._lifecycle:
            if self._listener is not None:
                return
            if self._store is not None and self._clients:
                await self._store.import_legacy(self._clients)
            app = web.Application(client_max_size=_MAX_BODY, middlewares=[self._route_errors])
            app.router.add_post("/v1/chat", self._chat, expect_handler=_reject_expect)
            runner = web.AppRunner(app, handler_cancellation=True, shutdown_timeout=2)
            await runner.setup()
            try:
                server = runner.server
                assert server is not None
                loop = asyncio.get_running_loop()

                def factory() -> _RequestHandler:
                    return _RequestHandler(
                        server,
                        loop=loop,
                        access_log=_PROTOCOL_LOG,
                        logger=_PROTOCOL_LOG,
                        auto_decompress=False,
                        lingering_time=0,
                        keepalive_timeout=5,
                    )

                listener = await loop.create_server(
                    lambda: _Connection(factory, self._connections, self._max_inflight + 32),
                    self._host,
                    self._port,
                    backlog=64,
                )
            except BaseException:
                await runner.cleanup()
                raise
            self._runner, self._listener = runner, listener

    async def stop(self) -> None:
        async with self._lifecycle:
            if self._listener is None:
                return
            listener, runner = self._listener, self._runner
            listener.close()
            for transport in self._connections:
                transport.abort()
            assert runner is not None
            try:
                await runner.cleanup()
                await listener.wait_closed()
            finally:
                try:
                    if self._validating:
                        await asyncio.shield(
                            asyncio.gather(*self._validating, return_exceptions=True)
                        )
                finally:
                    self._runner = self._listener = None

    @web.middleware
    async def _route_errors(
        self, request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]
    ) -> web.StreamResponse:
        timer = self._connections.get(request.transport) if request.transport is not None else None
        if timer is not None:
            timer.cancel()
        try:
            response: web.StreamResponse = await handler(request)
        except web.HTTPException as exc:
            headers = {"Allow": "POST"} if exc.status == 405 else None
            return _error(exc.status, "method_not_allowed" if headers else "not_found", headers)
        response.force_close()
        return response

    async def _authenticate(self, request: web.Request) -> str | None:
        values = request.headers.getall("Authorization", [])
        if len(values) != 1:
            return None
        scheme, _, token = values[0].partition(" ")
        if scheme.lower() != "bearer" or not _TOKEN.fullmatch(token):
            return None
        digest = hashlib.sha256(token.encode("ascii")).digest()
        if self._store is not None:
            async with asyncio.timeout(2):
                return await self._store.authenticate(digest)
        return self._clients.get(digest)

    def _admit(self, app_id: str) -> web.Response | None:
        now = asyncio.get_running_loop().time()
        recent = self._recent.setdefault(app_id, deque())
        while recent and recent[0] <= now - _RATE_WINDOW:
            recent.popleft()
        if len(recent) >= self._rate:
            retry = max(1, math.ceil(recent[0] + _RATE_WINDOW - now))
            return _error(429, "rate_limit", {"Retry-After": str(retry)})
        recent.append(now)
        if self._app_inflight.get(app_id, 0) >= self._max_per_app:
            return _error(429, "app_busy", {"Retry-After": "1"})
        if self._inflight >= self._max_inflight:
            return _error(503, "gateway_busy", {"Retry-After": "1"})
        return None

    async def _chat(self, request: web.Request) -> web.StreamResponse:
        try:
            app_id = await self._authenticate(request)
        except Exception:
            _LOG.warning("HTTP gateway app authentication unavailable")
            return _error(503, "gateway_busy", {"Retry-After": "1"})
        if app_id is None:
            return _error(401, "unauthorized", {"WWW-Authenticate": "Bearer"})
        if (rejected := self._admit(app_id)) is not None:
            return rejected
        self._inflight += 1
        self._app_inflight[app_id] = self._app_inflight.get(app_id, 0) + 1
        try:
            encodings = request.headers.getall("Content-Encoding", [])
            if encodings and [item.lower() for item in encodings] != ["identity"]:
                return _error(415, "unsupported_content_encoding")
            if request.content_type != "application/json" or request.charset not in (None, "utf-8"):
                return _error(415, "unsupported_media_type")
            if request.content_length is not None and request.content_length >= _MAX_BODY:
                return _error(413, "body_too_large")
            body = bytearray()
            try:
                async with asyncio.timeout(_BODY_TIMEOUT):
                    async for chunk in request.content.iter_chunked(64 * 1024):
                        if len(body) + len(chunk) >= _MAX_BODY:
                            return _error(413, "body_too_large")
                        body.extend(chunk)
            except TimeoutError:
                return _error(408, "body_timeout")
            except (OSError, RequestPayloadError, ValueError):
                return _error(400, "invalid_request")
            if len(self._validating) >= self._max_validating:
                return _error(503, "validation_busy", {"Retry-After": "1"})
            validation = asyncio.create_task(asyncio.to_thread(_validate, body, app_id))
            self._validating.add(validation)
            validation.add_done_callback(self._validation_finished)
            del body
            try:
                # A disconnected handler must not free a validation slot while its thread runs.
                inbound = await asyncio.shield(validation)
            except Exception:
                return _error(500, "validation_failed")
            if inbound is None:
                return _error(400, "invalid_request")
            if inbound.user_id in self._users_inflight:
                return _error(429, "user_busy", {"Retry-After": "1"})
            self._users_inflight.add(inbound.user_id)
            try:
                if self._store is None:
                    return await self._stream(request, inbound)
                try:
                    async with asyncio.timeout(2):
                        await self._store.record_request(
                            app_id, inbound.message_id, datetime.now(UTC)
                        )
                except Exception:
                    _LOG.warning("HTTP gateway usage ledger unavailable")
                    return _error(503, "gateway_busy", {"Retry-After": "1"})
                with usage_scope(self._store, app_id, inbound.message_id):
                    return await self._stream(request, inbound)
            finally:
                self._users_inflight.remove(inbound.user_id)
        finally:
            self._inflight -= 1
            self._app_inflight[app_id] -= 1
            if not self._app_inflight[app_id]:
                del self._app_inflight[app_id]

    def _validation_finished(self, task: asyncio.Task[InboundMessage | None]) -> None:
        self._validating.discard(task)
        if not task.cancelled() and task.exception() is not None:
            _LOG.warning("HTTP gateway request validation failed")

    async def _stream(self, request: web.Request, inbound: InboundMessage) -> web.StreamResponse:
        response = web.StreamResponse(
            headers={
                "Content-Type": "text/event-stream; charset=utf-8",
                "Cache-Control": "no-store, no-transform",
                "X-Accel-Buffering": "no",
            }
        )
        response.force_close()
        progress_count = 0
        accepting_progress = True

        async def progress(outbound: OutboundMessage) -> None:
            nonlocal progress_count
            if accepting_progress and progress_count < _MAX_PROGRESS:
                progress_count += 1
                await _write(response, _event("progress", {"content": outbound.content[:2_000]}))

        try:
            async with asyncio.timeout(_WRITE_TIMEOUT):
                await response.prepare(request)
            await _write(response, _event("start", {"message_id": inbound.message_id}))
            inbound.progress_sink = progress
            deadline = asyncio.timeout(self._timeout)
            try:
                async with deadline:
                    outbound = await self._handle(inbound)
                if deadline.expired():
                    raise TimeoutError
                if outbound is None:
                    terminal = _event("silent", {})
                else:
                    if (
                        len(outbound.content) > 65_536
                        or len(outbound.buttons) > 50
                        or any(
                            len(button.label) > 4_000 or len(button.command) > 4_000
                            for button in outbound.buttons
                        )
                    ):
                        raise ValueError("reply too large")
                    terminal = _event(
                        "reply",
                        {
                            "content": outbound.content,
                            "markdown": outbound.markdown or bool(outbound.buttons),
                            "buttons": [
                                {"label": b.label, "command": b.command} for b in outbound.buttons
                            ],
                        },
                    )
                    if len(terminal) > _MAX_FRAME:
                        raise ValueError("reply too large")
            except _StreamClosed:
                raise
            except Exception:
                code = "timeout" if deadline.expired() else "processing_failed"
                if code == "processing_failed":
                    _LOG.warning("HTTP gateway chat processing failed")
                terminal = _event("error", {"code": code})
            finally:
                accepting_progress = False
                inbound.progress_sink = None
            await _write(response, terminal)
            await _write(response, _event("done", {}))
            async with asyncio.timeout(_WRITE_TIMEOUT):
                await response.write_eof()
        except (_StreamClosed, ConnectionError, TimeoutError):
            if request.transport is not None:
                request.transport.abort()
        return response


def create_gateway(context: GatewayContext) -> HttpGateway:
    return HttpGateway(context)
