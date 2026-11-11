"""网页对话：不经过 QQ，在管理后台里直接和机器人说话。

走的是和 QQ 消息完全一样的处理流程（路由、命令、对话入口、查证工作流、出站过滤），
只有三处不同：消息标着 ``channel = web``，不给旁听的插件看（不进群聊记录）；
进度消息和回复不发给 QQ，而是一条条推给网页；图片是网页上传的，直接带着内容，不用下载。

网页上的身份是独立的（会话标识以 ``web-`` 开头）：它的对话历史、记忆和 QQ 上的用户互不相干。

回应是一条事件流（text/event-stream）：``progress``（一句进度）、``reply``（最终回复）、
``silent``（处理了但没有要回的）、``error``、最后一定有 ``done``（带处理记录的编号，
可以点过去看这条消息是怎么处理的）。
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import time
import uuid
from datetime import datetime
from typing import Any

from aiohttp import web

from jianshanagent.admin.core import Admin, fail, read_body, reply
from jianshanagent.core.logging import get_logger
from jianshanagent.core.messages import (
    CHANNEL_WEB,
    ChatScope,
    ImageRef,
    InboundMessage,
    OutboundMessage,
)
from jianshanagent.core.trace import brief

logger = get_logger(__name__)

_MAX_IMAGES = 4
_MAX_IMAGE_BYTES = 10 * 1024 * 1024
_MAX_TEXT_CHARS = 4000
_DATA_URI = re.compile(r"^data:(image/[\w.+-]+);base64,(.*)$", re.DOTALL)
_NAME = re.compile(r"[^\w\-]+")


def _identity(admin: Admin, body: dict[str, Any]) -> tuple[ChatScope, str, str, str]:
    """网页上的身份 →（会话类型，会话标识，用户标识，昵称）。"""
    name = _NAME.sub("", str(body.get("user") or "").strip())[:32] or admin.settings.web_chat_user
    user_id = f"web-{name}"
    if str(body.get("scope") or "c2c") == "group":
        return ChatScope.GROUP, "web-group", user_id, name
    return ChatScope.C2C, user_id, user_id, name


def _images(raw: Any) -> list[ImageRef]:
    if not raw:
        return []
    if not isinstance(raw, list) or len(raw) > _MAX_IMAGES:
        fail(400, f"一条消息最多带 {_MAX_IMAGES} 张图片")
    refs: list[ImageRef] = []
    for number, item in enumerate(raw, start=1):
        match = _DATA_URI.match(item) if isinstance(item, str) else None
        if match is None:
            fail(400, f"第 {number} 张图片不是能识别的格式")
        try:
            data = base64.b64decode(match.group(2), validate=True)
        except (binascii.Error, ValueError):
            fail(400, f"第 {number} 张图片的内容不完整")
        if len(data) > _MAX_IMAGE_BYTES:
            fail(400, f"第 {number} 张图片超过 {_MAX_IMAGE_BYTES // 1024 // 1024} MB")
        refs.append(
            ImageRef(
                url="",
                content_type=match.group(1),
                size=len(data),
                filename=f"upload-{number}",
                data=data,
            )
        )
    return refs


def _outbound(message: OutboundMessage) -> dict[str, Any]:
    return {
        "content": message.content,
        "markdown": message.markdown or bool(message.buttons),
        "buttons": [{"label": b.label, "command": b.command} for b in message.buttons],
    }


def routes(admin: Admin) -> list[web.RouteDef]:
    async def send(request: web.Request) -> web.StreamResponse:
        body = await read_body(request)
        text = str(body.get("text") or "").strip()
        images = _images(body.get("images"))
        if not text and not images:
            fail(400, "消息是空的")
        if len(text) > _MAX_TEXT_CHARS:
            fail(400, f"消息太长了（最多 {_MAX_TEXT_CHARS} 个字）")
        scope, chat_id, user_id, name = _identity(admin, body)
        message = InboundMessage(
            message_id=f"web-{uuid.uuid4().hex}",
            chat_scope=scope,
            chat_id=chat_id,
            user_id=user_id,
            content=text,
            timestamp=datetime.now().astimezone().isoformat(),
            user_name=name,
            group_id=chat_id if scope == ChatScope.GROUP else None,
            images=images,
            channel=CHANNEL_WEB,
        )

        response = web.StreamResponse(
            headers={
                "Content-Type": "text/event-stream; charset=utf-8",
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            }
        )
        await response.prepare(request)
        open_ = True

        async def emit(event: str, data: dict[str, Any]) -> None:
            """推一个事件。网页那边已经关了（刷新、离开）就不推了，处理照常做完。"""
            nonlocal open_
            if not open_:
                return
            payload = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
            try:
                await response.write(payload.encode("utf-8"))
            except (ConnectionError, RuntimeError):
                open_ = False

        async def progress(outbound: OutboundMessage) -> None:
            await emit("progress", {"content": outbound.content})

        message.progress_sink = progress
        started = time.perf_counter()
        await emit("start", {"message_id": message.message_id, "session": message.session_key})
        try:
            outbound = await admin.runtime.handle(message)
        except Exception as exc:
            logger.exception("web_chat_failed")
            await emit("error", {"message": brief(f"{type(exc).__name__}: {exc}", 500)})
        else:
            if outbound is None:
                await emit("silent", {})
            else:
                await emit("reply", _outbound(outbound))
        # 这条消息的处理记录（刚结束，还在内存里），页面上可以点过去看
        trace_id = next(
            (
                trace.id
                for trace in reversed(admin.runtime.tracer.recent)
                if trace.message_id == message.message_id
            ),
            None,
        )
        await emit("done", {"trace_id": trace_id, "seconds": time.perf_counter() - started})
        if open_:
            await response.write_eof()
        return response

    async def reset(request: web.Request) -> web.Response:
        """清空网页上这个身份的对话历史（等于它自己发了一句 /新话题）。"""
        body = await read_body(request)
        scope, chat_id, user_id, _ = _identity(admin, body)
        probe = InboundMessage(
            message_id="-", chat_scope=scope, chat_id=chat_id, user_id=user_id, content=""
        )
        store = admin.service("conversation_store")
        deleted = await store.clear(probe.session_key) if store is not None else 0
        return reply({"session": probe.session_key, "deleted": deleted})

    return [web.post("/api/chat", send), web.post("/api/chat/reset", reset)]
