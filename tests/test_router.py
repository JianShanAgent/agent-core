import pytest

from jianshanagent.core.config import load_config
from jianshanagent.core.messages import ChatScope, InboundMessage
from jianshanagent.core.plugin import PluginHost
from jianshanagent.core.router import MessageRouter


@pytest.fixture
def router(monkeypatch: pytest.MonkeyPatch) -> MessageRouter:
    cfg = load_config()
    # 单元测试不打真实 wiki：替换命令处理器前先加载插件
    host = PluginHost(cfg)

    async def fake_lookup(message: InboundMessage, args: str):
        from jianshanagent.core.messages import OutboundMessage

        return OutboundMessage(content=f"HIT:{args}", reply_to=message.message_id)

    host.load()
    # 覆盖查命令
    for key, spec in list(host.commands.items()):
        if spec.plugin_id == "wiki_query":
            host.commands[key] = type(spec)(
                name=spec.name,
                handler=fake_lookup,
                aliases=spec.aliases,
                description=spec.description,
                plugin_id=spec.plugin_id,
            )
    return MessageRouter(host, cfg.get("core.router"))


@pytest.mark.asyncio
async def test_route_command(router: MessageRouter) -> None:
    msg = InboundMessage(
        message_id="m1",
        chat_scope=ChatScope.GROUP,
        chat_id="g1",
        user_id="u1",
        content="/查 山风",
    )
    out = await router.dispatch(msg)
    assert out is not None
    assert out.content == "HIT:山风"


@pytest.mark.asyncio
async def test_cancel_session(router: MessageRouter) -> None:
    import time

    from jianshanagent.core.router import ActiveSession

    msg = InboundMessage(
        message_id="m2",
        chat_scope=ChatScope.C2C,
        chat_id="u1",
        user_id="u1",
        content="/取消",
    )
    router.set_session(
        msg.session_key,
        ActiveSession(kind="slot", plugin_id="demo", expires_at=time.time() + 60),
    )
    out = await router.dispatch(msg)
    assert out is not None
    assert "取消" in out.content


@pytest.mark.asyncio
async def test_progress_messages_pass_the_same_outbound_filters(router: MessageRouter) -> None:
    from jianshanagent.core.messages import OutboundMessage

    async def chat(message: InboundMessage) -> OutboundMessage:
        await message.progress("正在查\x07…")
        return OutboundMessage(content="结论\x07", reply_to=message.message_id)

    router.set_chat_handler(chat)
    sent: list[OutboundMessage] = []

    async def sink(progress: OutboundMessage) -> None:
        sent.append(progress)

    msg = InboundMessage(
        message_id="m3", chat_scope=ChatScope.C2C, chat_id="u1", user_id="u1", content="你好"
    )
    msg.progress_sink = sink
    out = await router.handle(msg)
    # 出站过滤（safety 插件去掉控制字符）对中途的进度消息和最终回复一视同仁
    assert [(p.content, p.reply_to, p.markdown) for p in sent] == [("正在查…", "m3", False)]
    assert out is not None and out.content == "结论"

    # 没有出口（网关不支持）时，报进度什么都不做
    bare = InboundMessage(
        message_id="m4", chat_scope=ChatScope.C2C, chat_id="u1", user_id="u1", content="你好"
    )
    assert (await router.handle(bare)) is not None and len(sent) == 1


@pytest.mark.asyncio
async def test_observers_see_every_message_but_unaddressed_ones_go_no_further(
    router: MessageRouter,
) -> None:
    from jianshanagent.core.messages import OutboundMessage

    seen: list[tuple[str, bool]] = []
    handled: list[str] = []

    async def observe(message: InboundMessage) -> None:
        seen.append((message.content, message.addressed))
        if "炸" in message.content:
            raise RuntimeError("boom")

    async def chat(message: InboundMessage) -> OutboundMessage:
        handled.append(message.content)
        return OutboundMessage(content="好的", reply_to=message.message_id)

    router.host.observers.append(observe)
    router.set_chat_handler(chat)

    def message(content: str, *, addressed: bool) -> InboundMessage:
        return InboundMessage(
            message_id=f"m-{content}",
            chat_scope=ChatScope.GROUP,
            chat_id="g1",
            user_id="u1",
            content=content,
            addressed=addressed,
        )

    # 群里没 @ 机器人的发言：旁听的插件看得到，但不进命令和对话，也不占限流的次数
    for _ in range(50):
        assert await router.handle(message("/查 山风", addressed=False)) is None
    assert handled == [] and len(seen) == 50
    # 对机器人说的照常处理；旁听的插件出错不影响回复
    asked = message("炸了吗", addressed=True)
    out = await router.handle(asked)
    assert out is not None and out.content == "好的"
    assert handled == ["炸了吗"] and seen[-1] == ("炸了吗", True)

    # 网关把最终回复发出去以后，旁听回复的插件也看一眼；某一个出错不影响别的
    replies: list[tuple[str, str]] = []

    async def broken(_inbound: InboundMessage, _outbound: OutboundMessage) -> None:
        raise RuntimeError("boom")

    async def observe_reply(inbound: InboundMessage, outbound: OutboundMessage) -> None:
        replies.append((inbound.content, outbound.content))

    router.host.reply_observers += [broken, observe_reply]
    await router.sent(asked, out)
    assert replies == [("炸了吗", "好的")]
