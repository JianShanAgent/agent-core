from jianshanagent.core.config import AppConfig
from jianshanagent.core.messages import ChatScope, InboundMessage
from jianshanagent.core.plugin import PluginHost
from jianshanagent.core.ratelimit import RateLimiter
from jianshanagent.core.router import MessageRouter


def test_sliding_window_and_single_notice() -> None:
    now = [0.0]
    limiter = RateLimiter(window_seconds=60, clock=lambda: now[0])

    assert [limiter.allow("u", 2) for _ in range(3)] == [True, True, False]
    assert [limiter.should_notify("u"), limiter.should_notify("u")] == [True, False]
    assert limiter.allow("other", 2) is True  # 各算各的
    now[0] = 61.0
    assert limiter.allow("u", 2) is True  # 窗口滑过后恢复
    assert limiter.allow("u", 0) is True  # 0 表示不限


async def test_router_blocks_over_limit_and_notifies_once(tmp_path) -> None:
    config = AppConfig(root=tmp_path, files={"core.plugins": {"enabled": []}}, secrets={})
    host = PluginHost(config)
    host.load()
    router = MessageRouter(host, {"rate_limit": {"per_user_per_minute": 2}})

    def message(user: str) -> InboundMessage:
        return InboundMessage(
            message_id="m", chat_scope=ChatScope.C2C, chat_id=user, user_id=user, content="你好"
        )

    replies = [await router.dispatch(message("u1")) for _ in range(4)]
    texts = [r.content if r else None for r in replies]

    assert texts[:2] == ["现在没有可用的命令。"] * 2  # 一个插件都没装
    assert texts[2] is not None and "消息有点多" in texts[2]
    assert texts[3] is None  # 第二次超限不再提示
    assert (await router.dispatch(message("u2"))) is not None  # 别的用户不受影响
