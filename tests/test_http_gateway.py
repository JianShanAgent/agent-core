from __future__ import annotations

import asyncio
import base64
import io
import json
import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import pytest
from aiohttp import ClientResponse, ClientSession, ClientTimeout
from PIL import Image

from jianshanagent.core.messages import (
    CHANNEL_HTTP,
    Button,
    ChatScope,
    InboundMessage,
    OutboundMessage,
)
from jianshanagent.gateway import http
from jianshanagent.gateway.base import GatewayContext

_TOKEN_A = "a" * 32
_TOKEN_B = "b" * 32
_PAYLOAD = {"user_id": "玩家", "text": "你好"}


async def _unexpected_sent(_: InboundMessage, __: OutboundMessage) -> None:
    pytest.fail("HTTP replies must not notify reply observers")


async def _silent(_: InboundMessage) -> OutboundMessage | None:
    return None


def _context(
    handle: Callable[[InboundMessage], Awaitable[OutboundMessage | None]] = _silent,
    **settings: Any,
) -> GatewayContext:
    return GatewayContext(
        settings={"port": 0, **settings},
        secrets={"clients": {"app-a": _TOKEN_A, "app-b": _TOKEN_B}},
        handle=handle,
        sent=_unexpected_sent,
    )


@asynccontextmanager
async def _running(context: GatewayContext) -> AsyncIterator[tuple[http.HttpGateway, str]]:
    gateway = http.create_gateway(context)
    assert not gateway.health()[0]
    await gateway.start()
    await gateway.start()
    assert gateway.health()[0]
    listener = gateway._listener
    assert listener is not None
    port = listener.sockets[0].getsockname()[1]
    try:
        yield gateway, f"http://127.0.0.1:{port}/v1/chat"
    finally:
        await gateway.stop()
        assert not gateway.health()[0]


def _auth(token: str = _TOKEN_A) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _client() -> ClientSession:
    return ClientSession(timeout=ClientTimeout(total=3))


async def _next_event(response: ClientResponse) -> tuple[str, dict[str, Any]]:
    event = (await response.content.readline()).decode()
    data = (await response.content.readline()).decode()
    assert event.startswith("event: ") and data.startswith("data: ")
    assert await response.content.readline() == b"\n"
    return event.removeprefix("event: ").strip(), json.loads(data.removeprefix("data: "))


async def test_sse_delivers_live_progress_then_reply_and_done() -> None:
    release = asyncio.Event()

    async def handle(message: InboundMessage) -> OutboundMessage:
        await message.progress("正在处理\nevent: fake")
        await release.wait()
        return OutboundMessage(
            "**结果**",
            buttons=[Button("继续", "/继续")],
            metadata={"trace": "private"},
        )

    async with _running(_context(handle)) as (_, url), _client() as client:
        async with client.post(url, json=_PAYLOAD, headers=_auth()) as response:
            assert response.status == 200
            assert response.content_type == "text/event-stream"
            assert response.headers["Cache-Control"] == "no-store, no-transform"
            event, start = await _next_event(response)
            assert event == "start" and start["message_id"].startswith("http-")
            assert set(start) == {"message_id"}
            assert await _next_event(response) == ("progress", {"content": "正在处理\nevent: fake"})
            release.set()
            assert await _next_event(response) == (
                "reply",
                {
                    "content": "**结果**",
                    "markdown": True,
                    "buttons": [{"label": "继续", "command": "/继续"}],
                },
            )
            assert await _next_event(response) == ("done", {})
            assert await response.read() == b""


async def test_authentication_and_rate_limit_precede_reading_body() -> None:
    async with _running(_context(requests_per_minute=1)) as (_, url), _client() as client:
        address = url.removeprefix("http://").split("/")[0]
        host, port = address.split(":")
        async with client.post(url, headers=_auth("x" * 32), json=_PAYLOAD) as response:
            assert response.status == 401
            assert response.headers["WWW-Authenticate"] == "Bearer"
        async with client.post(url + "?token=" + _TOKEN_A, json=_PAYLOAD) as response:
            assert response.status == 401
        duplicate = list(_auth().items()) * 2
        async with client.post(url, headers=duplicate, json=_PAYLOAD) as response:
            assert response.status == 401
        async with client.post(url, headers=_auth(), json=_PAYLOAD) as response:
            assert response.status == 200
            await response.read()
        cases = [("", b"401"), (f"Authorization: Bearer {_TOKEN_A}\r\n", b"429")]
        for authorization, expected in cases:
            reader, writer = await asyncio.open_connection(host, int(port))
            try:
                request = (
                    f"POST /v1/chat HTTP/1.1\r\nHost: {address}\r\n{authorization}"
                    "Content-Type: application/json\r\nContent-Length: 99\r\n\r\n"
                )
                writer.write(request.encode())
                async with asyncio.timeout(1):
                    assert (await reader.readline()).split()[1] == expected
            finally:
                writer.close()
                await writer.wait_closed()
        async with client.post(url, headers=_auth(_TOKEN_B), json=_PAYLOAD) as response:
            assert response.status == 200
            await response.read()


async def test_listener_rejects_expect_and_incomplete_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(http, "_HEADER_TIMEOUT", 0.1)
    async with _running(_context()) as (_, url), _client() as client:
        async with client.post(url, json=_PAYLOAD, expect100=True) as response:
            assert response.status == 417
            assert await response.json() == {"error": "expectation_not_supported"}
        async with client.get(url.replace("/v1/chat", "/api/chat")) as response:
            assert response.status == 404
        host, port = url.removeprefix("http://").split("/")[0].split(":")
        reader, writer = await asyncio.open_connection(host, int(port))
        try:
            writer.write(b"POST /v1/chat HTTP/1.1\r\ninvalid-private-header\r\n\r\n")
            raw_response = await asyncio.wait_for(reader.read(), 1)
            assert raw_response.startswith(b"HTTP/1.0 400") or raw_response.startswith(
                b"HTTP/1.1 400"
            )
            assert b"invalid-private-header" not in raw_response
        finally:
            writer.close()
            await writer.wait_closed()
        reader, writer = await asyncio.open_connection(host, int(port))
        try:
            writer.write(b"POST /v1/chat HTTP/1.1\r\n")
            assert await asyncio.wait_for(reader.read(), 1) == b""
        finally:
            writer.close()
            await writer.wait_closed()


async def test_identity_scopes_app_user_and_conversation() -> None:
    seen: list[InboundMessage] = []

    async def handle(message: InboundMessage) -> None:
        seen.append(message)

    async with _running(_context(handle)) as (_, url), _client() as client:
        variants = [
            (_TOKEN_A, "u:1", None),
            (_TOKEN_A, "u:1", None),
            (_TOKEN_A, "u:1", "room"),
            (_TOKEN_B, "u:1", "room"),
            (_TOKEN_A, "u:2", "room"),
            (_TOKEN_A, "u:1", "default"),
        ]
        for token, user, room in variants:
            body = {"user_id": user, "text": "hello"}
            if room is not None:
                body["conversation_id"] = room
            async with client.post(url, json=body, headers=_auth(token)) as response:
                assert response.status == 200
                assert "event: silent\ndata: {}" in await response.text()
    first, repeat, room, other_app, other_user, named_default = seen
    assert all(m.chat_scope is ChatScope.C2C and m.channel == CHANNEL_HTTP for m in seen)
    assert len({m.message_id for m in seen}) == len(seen)
    assert first.session_key == repeat.session_key
    assert first.user_id == room.user_id == named_default.user_id
    assert len({m.session_key for m in (first, room, other_app, other_user, named_default)}) == 5
    assert first.user_suffix and room.session_key.endswith(first.user_suffix)
    assert not other_app.session_key.endswith(first.user_suffix)
    assert not other_user.session_key.endswith(first.user_suffix)


async def test_disconnect_cancels_handler_and_releases_app_slot() -> None:
    stopped = asyncio.Event()

    async def handle(message: InboundMessage) -> None:
        if message.content == "wait":
            try:
                await message.progress("running")
                await asyncio.Future()
            finally:
                stopped.set()

    async with _running(_context(handle, max_inflight_per_app=1)) as (_, url), _client() as client:
        async with client.post(url, headers=_auth(), json={**_PAYLOAD, "text": "wait"}) as response:
            assert (await _next_event(response))[0] == "start"
            assert (await _next_event(response))[0] == "progress"
            response.close()
        await asyncio.wait_for(stopped.wait(), 1)
        async with client.post(url, headers=_auth(), json=_PAYLOAD) as response:
            assert response.status == 200
            await response.read()


async def test_independent_app_slots_and_global_limit() -> None:
    release = asyncio.Event()

    async def handle(_: InboundMessage) -> None:
        await release.wait()

    ctx = _context(handle, max_inflight_per_app=1, max_inflight=2)
    ctx.secrets["clients"]["app-c"] = "c" * 32
    async with _running(ctx) as (_, url), _client() as client:
        async with client.post(url, headers=_auth(), json=_PAYLOAD) as a:
            assert a.status == 200
            async with client.post(url, headers=_auth(), json=_PAYLOAD) as response:
                assert response.status == 429
                assert await response.json() == {"error": "app_busy"}
            async with client.post(url, headers=_auth(_TOKEN_B), json=_PAYLOAD) as b:
                assert b.status == 200
                async with client.post(url, headers=_auth("c" * 32), json=_PAYLOAD) as response:
                    assert response.status == 503
                    assert await response.json() == {"error": "gateway_busy"}
                release.set()
                await b.read()
            await a.read()


async def test_same_app_user_cannot_run_two_conversations() -> None:
    release = asyncio.Event()

    async def handle(message: InboundMessage) -> None:
        if message.content == "wait":
            await release.wait()

    async with _running(_context(handle)) as (_, url), _client() as client:
        pending = {**_PAYLOAD, "text": "wait", "conversation_id": "original"}
        async with client.post(url, json=pending, headers=_auth()) as first:
            assert (await _next_event(first))[0] == "start"
            other_conversation = {**_PAYLOAD, "conversation_id": "different"}
            async with client.post(url, json=other_conversation, headers=_auth()) as blocked:
                assert blocked.status == 429
                assert await blocked.json() == {"error": "user_busy"}
            for token, user in [(_TOKEN_A, "another-user"), (_TOKEN_B, _PAYLOAD["user_id"])]:
                body = {**other_conversation, "user_id": user}
                async with client.post(url, json=body, headers=_auth(token)) as allowed:
                    assert allowed.status == 200
                    await allowed.read()
            release.set()
            await first.read()
        async with client.post(url, json=other_conversation, headers=_auth()) as allowed:
            assert allowed.status == 200
            await allowed.read()


async def test_validation_capacity_survives_disconnect_and_stop_waits_for_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    validation_started = asyncio.Event()
    request_finished = asyncio.Event()
    unblock = threading.Event()
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    original_validate = http._validate
    original_chat = http.HttpGateway._chat

    def paused_validate(body: bytearray, app: str) -> InboundMessage | None:
        assert threading.get_ident() != loop_thread
        loop.call_soon_threadsafe(validation_started.set)
        assert unblock.wait(3)
        return original_validate(body, app)

    async def observed_chat(gateway: http.HttpGateway, request: Any) -> Any:
        try:
            return await original_chat(gateway, request)
        finally:
            request_finished.set()

    monkeypatch.setattr(http, "_validate", paused_validate)
    monkeypatch.setattr(http.HttpGateway, "_chat", observed_chat)
    async with _running(_context(max_inflight=1)) as (gateway, url), _client() as client:
        stopping = None
        try:
            host, port = url.removeprefix("http://").split("/")[0].split(":")
            _, writer = await asyncio.open_connection(host, int(port))
            try:
                body = json.dumps(_PAYLOAD).encode()
                header = (
                    f"POST /v1/chat HTTP/1.1\r\nHost: {host}:{port}\r\n"
                    f"Authorization: Bearer {_TOKEN_A}\r\nContent-Type: application/json\r\n"
                    f"Content-Length: {len(body)}\r\n\r\n"
                )
                writer.write(header.encode() + body)
                await asyncio.wait_for(validation_started.wait(), 1)
            finally:
                writer.close()
                await writer.wait_closed()
            await asyncio.wait_for(request_finished.wait(), 1)
            async with client.post(url, json=_PAYLOAD, headers=_auth(_TOKEN_B)) as busy:
                assert busy.status == 503
                assert await busy.json() == {"error": "validation_busy"}
            stopping = asyncio.create_task(gateway.stop())
            done, _ = await asyncio.wait({stopping}, timeout=0.05)
            assert not done and not gateway.health()[0]
        finally:
            unblock.set()
            if stopping is not None:
                await asyncio.wait_for(stopping, 1)


async def test_timeout_and_processing_error_do_not_leak_details(
    caplog: pytest.LogCaptureFixture,
) -> None:
    cancelled = asyncio.Event()

    async def handle(message: InboundMessage) -> None:
        if message.content == "error":
            raise RuntimeError("private request and diagnostics")
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    async with _running(_context(handle, timeout_seconds=0.1)) as (_, url), _client() as client:
        for text, code in [("error", "processing_failed"), ("wait", "timeout")]:
            body = {**_PAYLOAD, "text": text}
            async with client.post(url, json=body, headers=_auth()) as response:
                assert (await _next_event(response))[0] == "start"
                assert await _next_event(response) == ("error", {"code": code})
                assert await _next_event(response) == ("done", {})
    assert cancelled.is_set()
    assert "private request" not in caplog.text


def _image_uri(format_name: str, size: tuple[int, int] = (2, 3)) -> str:
    stream = io.BytesIO()
    Image.new("RGB", size).save(stream, format=format_name)
    data = base64.b64encode(stream.getvalue()).decode("ascii")
    return f"data:image/{format_name.lower()};base64,{data}"


async def test_accepts_bounded_inline_images_and_rejects_remote_or_bad_images() -> None:
    seen: list[InboundMessage] = []

    async def handle(message: InboundMessage) -> None:
        seen.append(message)

    png = _image_uri("PNG")
    jpeg = _image_uri("JPEG")
    async with _running(_context(handle)) as (_, url), _client() as client:
        async with client.post(
            url, json={"user_id": "u", "images": [png, jpeg]}, headers=_auth()
        ) as response:
            assert response.status == 200
            await response.read()
        for value in (
            "https://example.invalid/image.png",
            "data:image/png;base64,@@@@",
            png.replace("image/png", "image/jpeg"),
            _image_uri("PNG", (4096, 2048)),
        ):
            async with client.post(
                url, json={**_PAYLOAD, "images": [value]}, headers=_auth()
            ) as response:
                assert response.status == 400
        async with client.post(
            url, json={**_PAYLOAD, "images": [png] * 5}, headers=_auth()
        ) as response:
            assert response.status == 400
    assert len(seen) == 1 and seen[0].content == ""
    assert [img.content_type for img in seen[0].images] == ["image/png", "image/jpeg"]
    assert all(
        not img.url and img.data and img.width == 2 and img.height == 3 for img in seen[0].images
    )


async def test_body_limits_and_content_encoding(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(http, "_MAX_BODY", 128)
    async with _running(_context()) as (_, url), _client() as client:
        headers = {**_auth(), "Content-Type": "application/json"}
        async with client.post(url, data=b"x" * 129, headers=headers) as response:
            assert response.status == 413

        async def chunks() -> AsyncIterator[bytes]:
            yield b"x" * 64
            yield b"x" * 64
            yield b"x"

        async with client.post(url, data=chunks(), headers=headers) as response:
            assert response.status == 413
        async with client.post(
            url, data=b"not a gzip stream", headers={**headers, "Content-Encoding": "gzip"}
        ) as response:
            assert response.status == 415
        async with client.post(
            url, data=b'{"user_id":"a","user_id":"b","text":"x"}', headers=headers
        ) as response:
            assert response.status == 400


async def test_stop_cancels_active_stream_and_allows_restart() -> None:
    stopped = asyncio.Event()

    async def handle(message: InboundMessage) -> None:
        try:
            await message.progress("running")
            await asyncio.Future()
        finally:
            stopped.set()

    async with _running(_context(handle)) as (gateway, url), _client() as client:
        async with client.post(url, json=_PAYLOAD, headers=_auth()) as response:
            assert (await _next_event(response))[0] == "start"
            assert (await _next_event(response))[0] == "progress"
            await asyncio.wait_for(gateway.stop(), 1)
            assert stopped.is_set()
            assert not gateway.health()[0]
            await gateway.start()
            assert gateway.health()[0]


@pytest.mark.parametrize(
    "settings",
    [
        {"host": "https://example.invalid"},
        {"port": -1},
        {"requests_per_minute": 0},
        {"max_inflight_per_app": True},
        {"max_inflight": 65},
        {"timeout_seconds": float("nan")},
    ],
)
def test_invalid_settings(settings: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        http.create_gateway(_context(**settings))


@pytest.mark.parametrize(
    "clients",
    [
        {},
        {"app-a": "too-short"},
        {"bad app": _TOKEN_A},
        {"app-a": _TOKEN_A, "app-b": _TOKEN_A},
    ],
)
def test_invalid_clients(clients: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        http.create_gateway(
            GatewayContext(
                settings={}, secrets={"clients": clients}, handle=_silent, sent=_unexpected_sent
            )
        )
