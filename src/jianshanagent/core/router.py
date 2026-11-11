from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

from jianshanagent.core import trace
from jianshanagent.core.logging import get_logger
from jianshanagent.core.messages import CHANNEL_WEB, ChatScope, InboundMessage, OutboundMessage
from jianshanagent.core.plugin import PluginHost
from jianshanagent.core.ratelimit import RateLimiter

logger = get_logger(__name__)


@dataclass
class ActiveSession:
    kind: str
    plugin_id: str
    data: dict[str, Any] = field(default_factory=dict)
    expires_at: float = 0.0


class MessageRouter:
    """命令 > 活动会话 > 工作流触发 > 闲聊。"""

    def __init__(self, host: PluginHost, router_cfg: dict[str, Any]):
        self.host = host
        self.prefixes = list(router_cfg.get("command_prefixes") or ["/"])
        active = router_cfg.get("active_session") or {}
        self.session_ttl = int(active.get("ttl_seconds") or 600)
        self.cancel_commands = {
            c.lstrip("/").strip().lower()
            for c in (active.get("cancel_commands") or ["/取消", "/cancel"])
        }
        self._sessions: dict[str, ActiveSession] = {}
        self._fallback_chat = host.chat_handler  # 由 chat 插件注册；没有则为 None
        limits = router_cfg.get("rate_limit") or {}
        self._user_limit = int(limits.get("per_user_per_minute") or 0)
        self._group_limit = int(limits.get("per_group_per_minute") or 0)
        self._limiter = RateLimiter(window_seconds=60)

    def _check_rate_limit(self, message: InboundMessage) -> tuple[bool, OutboundMessage | None]:
        """返回（是否超限，提示消息）。超限时每分钟只提示一次，其余静默。"""
        key = f"user:{message.session_key}"
        blocked = not self._limiter.allow(key, self._user_limit)
        if not blocked and message.chat_scope == ChatScope.GROUP:
            key = f"group:{message.chat_id}"
            blocked = not self._limiter.allow(key, self._group_limit)
        if not blocked:
            return False, None
        logger.info("rate_limited", key=key)
        if not self._limiter.should_notify(key):
            return True, None
        notice = "消息有点多，我歇一会儿，一分钟后再来。"
        return True, OutboundMessage(content=notice, reply_to=message.message_id)

    def set_chat_handler(self, handler: Any) -> None:
        self._fallback_chat = handler

    def clear_session(self, key: str) -> None:
        self._sessions.pop(key, None)

    def set_session(self, key: str, session: ActiveSession) -> None:
        session.expires_at = time.time() + self.session_ttl
        self._sessions[key] = session

    def _get_session(self, key: str) -> ActiveSession | None:
        session = self._sessions.get(key)
        if not session:
            return None
        if session.expires_at < time.time():
            self._sessions.pop(key, None)
            return None
        return session

    def _parse_command(self, content: str) -> tuple[str, str] | None:
        text = content.strip()
        for prefix in self.prefixes:
            if text.startswith(prefix):
                body = text[len(prefix) :].strip()
                if not body:
                    return None
                parts = body.split(maxsplit=1)
                name = parts[0].lower()
                args = parts[1] if len(parts) > 1 else ""
                return name, args
        # 无前缀但整句匹配已注册命令名（如「查 山风」）
        parts = text.split(maxsplit=1)
        if not parts:
            return None
        name = parts[0].lower()
        if name in self.host.commands:
            return name, parts[1] if len(parts) > 1 else ""
        return None

    async def dispatch(self, message: InboundMessage) -> OutboundMessage | None:
        for mw in self.host.inbound_middlewares:
            message_or_none = await mw(message)
            if message_or_none is None:
                logger.info("inbound_dropped", by="middleware", user_id=message.user_id)
                trace.note(route="dropped")
                return None
            message = message_or_none

        blocked, notice = self._check_rate_limit(message)
        if blocked:
            trace.note(route="limited")
            return notice

        parsed = self._parse_command(message.content)
        if parsed:
            name, args = parsed
            if name in self.cancel_commands:
                self.clear_session(message.session_key)
                trace.note(route="command", command=name)
                return OutboundMessage(content="已取消当前会话。", reply_to=message.message_id)
            cmd = self.host.commands.get(name)
            if cmd:
                logger.info("route_command", command=name, plugin=cmd.plugin_id)
                trace.note(route="command", command=cmd.name)
                with trace.span("command", f"/{cmd.name}", plugin=cmd.plugin_id, args=args):
                    return await cmd.handler(message, args)

        session = self._get_session(message.session_key)
        if session:
            logger.info("route_active_session", kind=session.kind, plugin=session.plugin_id)
            trace.note(route="session")
            # 工作流续跑将在后续里程碑挂入；目前仅占位清理过期
            return OutboundMessage(
                content="当前有未完成的会话，请继续补充信息，或发送 /取消。",
                reply_to=message.message_id,
            )

        for kw in self.host.keywords:
            if re.search(kw.pattern, message.content):
                logger.info("route_keyword", pattern=kw.pattern, plugin=kw.plugin_id)
                trace.note(route="keyword")
                with trace.span("keyword", kw.pattern, plugin=kw.plugin_id):
                    return await kw.handler(message)

        if self._fallback_chat is not None:
            logger.info("route_chat")
            trace.note(route="chat")
            return await self._fallback_chat(message)

        # 没有对话插件接手：群聊里不出声，私聊里告诉对方有哪些命令
        if message.chat_scope.value == "c2c":
            names = list(dict.fromkeys(spec.name for spec in self.host.commands.values()))
            listed = "、".join(f"/{name}" for name in names)
            return OutboundMessage(
                content=f"可用命令：{listed}" if listed else "现在没有可用的命令。",
                reply_to=message.message_id,
            )
        return None

    async def handle(self, message: InboundMessage) -> OutboundMessage | None:
        """一条消息从进到出：分发 + 出站过滤。处理中途发的进度消息也过同样的出站过滤。

        旁听的插件先看一眼每一条消息；不是对机器人说的（``addressed`` 为假）到此为止，
        不过限流、不进命令和对话、不回复。管理后台网页对话里发的消息（``channel`` 是 web）
        不给旁听的插件看：那是调试，不该混进群聊记录。

        对机器人说的每一条都留一份处理记录（core/trace.py）：走了哪条路、每一步多久、回了什么。
        """
        if message.channel != CHANNEL_WEB:
            for observe in self.host.observers:
                try:
                    await observe(message)
                except Exception:  # 旁听出错不影响回复
                    logger.exception("observer_failed")
        if not message.addressed:
            return None
        tracer: trace.Tracer | None = self.host.services.get("tracer")
        record = tracer.begin(message) if tracer is not None else None
        try:
            sink = message.progress_sink
            if sink is not None:

                async def filtered(progress: OutboundMessage) -> None:
                    checked = await self.apply_outbound(progress, message)
                    if checked is not None:
                        trace.event("progress", checked.content)
                        await sink(checked)

                message.progress_sink = filtered
            outbound = await self.dispatch(message)
            if outbound is not None:
                outbound = await self.apply_outbound(outbound, message)
        except BaseException as exc:
            if tracer is not None and record is not None:
                tracer.finish(record, error=exc)
            raise
        if tracer is not None and record is not None:
            tracer.finish(record, reply=outbound)
        return outbound

    async def sent(self, inbound: InboundMessage, outbound: OutboundMessage) -> None:
        """网关把最终回复发出去以后调用：给旁听的插件看一眼（比如记群聊记录）。"""
        for observe in self.host.reply_observers:
            try:
                await observe(inbound, outbound)
            except Exception:
                logger.exception("reply_observer_failed")

    async def apply_outbound(
        self,
        outbound: OutboundMessage,
        inbound: InboundMessage,
    ) -> OutboundMessage | None:
        current: OutboundMessage | None = outbound
        for filt in self.host.outbound_filters:
            if current is None:
                return None
            current = await filt(current, inbound)
        return current
