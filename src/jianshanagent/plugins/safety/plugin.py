"""safety 插件：出站兜底。

机器人发出去的每一条消息（最终回复和中途的进度）都过它：去掉控制字符，
命中拦截规则（正则）的整条不发，太长的截断。
"""

from __future__ import annotations

import re

from jianshanagent.core.logging import get_logger
from jianshanagent.core.messages import InboundMessage, OutboundMessage
from jianshanagent.core.plugin import PluginContext

logger = get_logger(__name__)


class SafetyPlugin:
    id = "safety"

    def register(self, ctx: PluginContext) -> None:
        cfg = ctx.plugin_config()
        if not cfg.get("enabled", True):
            return
        outbound = cfg.get("outbound") or {}
        max_chars = int(outbound.get("max_message_chars") or 1800)
        strip_ctrl = bool(outbound.get("strip_control_chars", True))
        patterns = [re.compile(p) for p in (outbound.get("block_patterns") or [])]

        async def filter_outbound(
            msg: OutboundMessage,
            _inbound: InboundMessage,
        ) -> OutboundMessage | None:
            content = msg.content or ""
            if strip_ctrl:
                content = "".join(ch for ch in content if ch == "\n" or ord(ch) >= 32)
            for pat in patterns:
                if pat.search(content):
                    # 记下是哪条规则拦的、多长的一条；内容本身不进日志
                    logger.warning("outbound_blocked", pattern=pat.pattern, chars=len(content))
                    return None
            if len(content) > max_chars:
                content = content[: max_chars - 1] + "…"
            msg.content = content
            return msg

        ctx.add_outbound_filter(filter_outbound)


def create_plugin() -> SafetyPlugin:
    return SafetyPlugin()
