"""safety 插件：发出去之前的最后一道。"""

from pathlib import Path

from structlog.testing import capture_logs

from jianshanagent.core.config import AppConfig
from jianshanagent.core.messages import ChatScope, InboundMessage, OutboundMessage
from jianshanagent.core.plugin import PluginContext
from jianshanagent.plugins.safety.plugin import SafetyPlugin


async def test_blocked_messages_are_dropped_and_logged_without_their_content() -> None:
    cfg = {
        "enabled": True,
        "outbound": {"max_message_chars": 10, "block_patterns": [r"内部\s*密码"]},
    }
    ctx = PluginContext(
        "safety", AppConfig(root=Path("."), files={"plugin.safety": cfg}, secrets={}), {}
    )
    SafetyPlugin().register(ctx)
    (outbound,) = ctx.outbound_filters
    asked = InboundMessage(
        message_id="m1", chat_scope=ChatScope.GROUP, chat_id="g", user_id="u", content="x"
    )

    with capture_logs() as logs:
        assert await outbound(OutboundMessage(content="内部 密码是 hunter2"), asked) is None
        passed = await outbound(OutboundMessage(content="一二三四五六七八九十多出来的"), asked)
    assert passed is not None and passed.content == "一二三四五六七八九…"  # 太长的截断，照发

    # 拦下来的要留一条日志（不然只看到机器人没回话，不知道为什么）：记哪条规则、多长，不记内容
    assert logs == [
        {
            "event": "outbound_blocked",
            "log_level": "warning",
            "pattern": r"内部\s*密码",
            "chars": 14,
        }
    ]
