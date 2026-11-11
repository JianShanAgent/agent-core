"""处理记录：一条消息走了哪几步、调了几次模型，怎么记下来、怎么落地。"""

import asyncio
from typing import Any

import httpx
import pytest
from structlog.contextvars import get_contextvars

from jianshanagent.core import trace
from jianshanagent.core.config import load_config
from jianshanagent.core.llm import LLMClient, LLMError, LLMProfile, LLMSettings, user_message
from jianshanagent.core.messages import CHANNEL_WEB, ChatScope, InboundMessage, OutboundMessage
from jianshanagent.core.plugin import CommandSpec, PluginHost, erase_user_data
from jianshanagent.core.router import MessageRouter
from jianshanagent.core.trace import LLMCall, Trace, Tracer


class MemorySink:
    def __init__(self, *, broken: bool = False) -> None:
        self.traces: list[Trace] = []
        self.calls: list[LLMCall] = []
        self.broken = broken

    async def save_trace(self, record: Trace) -> None:
        if self.broken:
            raise RuntimeError("数据库断了")
        self.traces.append(record)

    async def save_call(self, call: LLMCall) -> None:
        if self.broken:
            raise RuntimeError("数据库断了")
        await asyncio.sleep(0)  # 真的存盘是要等的：先让出去一下，别的事可以插进来
        self.calls.append(call)

    async def erase_trace(self, trace_id: str) -> None:
        self.traces = [record for record in self.traces if record.id != trace_id]
        self.calls = [call for call in self.calls if call.trace_id != trace_id]


def _message(content: str = "问题", **fields: Any) -> InboundMessage:
    return InboundMessage(
        message_id=fields.pop("message_id", "m1"),
        chat_scope=fields.pop("chat_scope", ChatScope.C2C),
        chat_id=fields.pop("chat_id", "c1"),
        user_id=fields.pop("user_id", "u1"),
        content=content,
        **fields,
    )


def _llm(handler: Any, tracer: Tracer | None = None) -> LLMClient:
    settings = LLMSettings(
        base_url="https://llm.example/v1",
        api_key="tp-test",
        profiles={
            "chat": LLMProfile("chat", "flash"),
            "reason": LLMProfile("reason", "pro", thinking=True),
        },
        max_retries=0,
    )
    client = LLMClient(settings, transport=httpx.MockTransport(handler), retry_base_seconds=0)
    if tracer is not None:
        client.on_call = tracer.record_call
    return client


def _ok(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "好的", "reasoning_content": "想"},
                }
            ],
            "model": "pro-0101",
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "completion_tokens_details": {"reasoning_tokens": 15},
            },
        },
    )


async def test_the_message_that_asks_to_delete_my_data_leaves_no_record() -> None:
    sink = MemorySink()
    tracer = Tracer(sink)
    llm = _llm(_ok, tracer)
    earlier = tracer.begin(_message("我住在杭州", message_id="m0"))
    await llm.chat([user_message("我住在杭州")])
    tracer.finish(earlier, reply=OutboundMessage(content="好的"))

    record = tracer.begin(_message("把我的信息都删掉"))
    await llm.chat([user_message("把我的信息都删掉")])  # 模型决定调删除工具的那一次：已经交去存了
    await erase_user_data([], _message("把我的信息都删掉"))  # 删除的入口（指令和工具都走它）
    assert record.forgotten
    await llm.chat([user_message("删完了，说一声")])  # 删完之后的那一次：不存
    tracer.finish(record, reply=OutboundMessage(content="都删掉了"))
    await tracer.drain()

    # 这条消息自己的记录和它名下的模型调用都没有留下；之前那条不受影响（那条由各插件的删除去管）
    assert [kept.id for kept in sink.traces] == [earlier.id]
    assert [call.trace_id for call in sink.calls] == [earlier.id]
    assert list(tracer.recent) == [earlier]  # 内存里「最近几条」也没有它
    assert trace.current() is None

    trace.forget_current()  # 不在处理消息时调用：什么都不做，不报错


async def test_log_lines_written_while_handling_a_message_carry_its_record_id() -> None:
    # 日志里不写用户说了什么（见 test_privacy.py），靠这个编号去处理记录里对上是哪个问题。
    # 编号是绑在 structlog 的上下文里的，日志管线的第一环（merge_contextvars）会把它并进每一行
    tracer = Tracer()
    assert "trace" not in get_contextvars()
    record = tracer.begin(_message("问题"))
    assert get_contextvars()["trace"] == record.id

    async def background() -> str:  # 处理过程中起的后台任务（发进度之类）也带着
        return str(get_contextvars().get("trace"))

    assert await asyncio.create_task(background()) == record.id
    tracer.finish(record, reply=None)
    assert "trace" not in get_contextvars()


async def test_steps_notes_and_model_calls_end_up_on_the_trace() -> None:
    sink = MemorySink()
    tracer = Tracer(sink)
    llm = _llm(_ok, tracer)
    record = tracer.begin(_message("千姬的 CV 是谁", user_name="阿岚"))
    assert trace.current() is record and tracer.active == {record.id: record}

    trace.note(route="research")
    trace.event("progress", "正在查资料…")
    with trace.span("analyst", "查证员写结论", evidence_count=3) as step:
        image = user_message("看图", images=["data:image/jpeg;base64,AAAA"])
        await llm.chat([image], profile="reason")
        step.set(action="answer")
    with pytest.raises(ValueError), trace.span("review", "复核"):
        raise ValueError("炸了")
    await llm.chat([user_message("不在任何一步里")])
    trace.note(outcome="answered", research={"rejections": 0})
    tracer.finish(record, reply=OutboundMessage(content="高垣彩阳"))
    await tracer.drain()

    assert trace.current() is None and tracer.active == {} and list(tracer.recent) == [record]
    assert sink.traces == [record] and len(sink.calls) == 2
    assert (record.route, record.status, record.outcome, record.reply) == (
        "research",
        trace.REPLIED,
        "answered",
        "高垣彩阳",
    )
    kinds = [(s.kind, s.title, s.ok, s.seconds is None) for s in record.steps]
    assert kinds == [
        ("progress", "正在查资料…", True, True),  # 一闪而过的事没有用时
        ("analyst", "查证员写结论", True, False),
        ("review", "复核", False, False),  # 出了错的一步：标出来，错误照样往外抛
    ]
    assert record.steps[1].data == {"evidence_count": 3, "action": "answer"}
    assert "炸了" in record.steps[2].data["error"]

    first, second = sink.calls
    # 模型调用记在它所在的那一步名下；图片不存，只留占位
    assert (first.trace_id, first.step_id, first.purpose) == (record.id, 2, "analyst")
    assert (first.profile, first.model, first.thinking, first.ok) == (
        "reason",
        "pro-0101",
        True,
        True,
    )
    assert (first.prompt_tokens, first.completion_tokens, first.reasoning_tokens) == (100, 20, 15)
    assert first.request is not None and first.request["messages"][0]["content"] == "[图片]看图"
    assert first.response == {"content": "好的", "reasoning": "想", "tool_calls": []}
    assert (second.trace_id, second.step_id, second.purpose) == (record.id, None, "")
    summary = record.to_json()
    assert (summary["llm_calls"], summary["prompt_tokens"], summary["reasoning_tokens"]) == (
        2,
        200,
        30,
    )
    assert summary["summary"] == {"research": {"rejections": 0}}


async def test_nothing_is_recorded_outside_a_message_but_the_code_still_runs() -> None:
    with trace.span("analyze", "分析问题", question="x") as step:
        step.set(topic="式神")  # 不在处理消息时：照常执行，填的东西不留
    trace.event("progress", "…")
    trace.note(route="research")
    assert trace.current() is None and step.id == 0

    # 不属于哪条消息的模型调用（管理后台里测试模型）也有用量记录，环节名用 purpose 指定
    sink = MemorySink()
    tracer = Tracer(sink, keep_prompts=False)
    llm = _llm(_ok, tracer)
    with trace.purpose("test"):
        await llm.chat([user_message("测试")])
    await tracer.drain()
    (call,) = sink.calls
    assert (call.trace_id, call.purpose, call.ok) == (None, "test", True)
    assert call.request is None and call.response is None  # 配置里关了「留提示词」


async def test_failed_and_abandoned_model_calls_are_recorded_too() -> None:
    sink = MemorySink()
    tracer = Tracer(sink)

    def refuse(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="Param Incorrect")

    async def hang(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(3600)
        raise AssertionError("不会走到这里")

    with pytest.raises(LLMError):
        await _llm(refuse, tracer).chat([user_message("x")])
    with pytest.raises(TimeoutError):  # 调用方限了时，到点不等了
        await asyncio.wait_for(_llm(hang, tracer).chat([user_message("x")], profile="reason"), 0.05)
    await tracer.drain()

    refused, abandoned = sink.calls
    assert (refused.ok, refused.profile) == (False, "chat") and "HTTP 400" in refused.error
    assert (abandoned.ok, abandoned.profile) == (False, "reason") and "不等了" in abandoned.error
    assert abandoned.seconds >= 0.04 and abandoned.prompt_tokens == 0


async def test_a_broken_sink_never_breaks_message_handling() -> None:
    tracer = Tracer(MemorySink(broken=True))
    record = tracer.begin(_message())
    tracer.finish(record, error=RuntimeError("处理时炸了"))
    await tracer.drain()  # 存不进去只记日志
    assert record.status == trace.ERROR and "处理时炸了" in record.error
    assert trace.current() is None

    def crash(_call: LLMCall) -> None:
        raise RuntimeError("记录的环节自己出错")

    llm = _llm(_ok)
    llm.on_call = crash
    assert (await llm.chat([user_message("x")])).content == "好的"


def test_stored_messages_drop_media_and_cap_length() -> None:
    long_text = "很长" * 40000
    compact = trace.compact_messages(
        [
            {"role": "system", "content": "人设"},
            user_message("这是谁", images=["data:image/png;base64,QUJD"]),
            {"role": "user", "content": long_text},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]},
        ]
    )
    assert compact[0] == {"role": "system", "content": "人设"}
    assert compact[1]["content"] == "[图片]这是谁"
    kept = compact[2]["content"]
    assert len(kept) < len(long_text) and "中间这 20000 字没有存" in kept
    # 留开头和结尾：提示词的结尾是这次要模型做什么（复核员的最后是查证员的回答），不能丢
    prompt = "开头的规则" + "资料" * 50000 + "查证员的回答：3 日开"
    kept = trace.compact_messages([{"role": "user", "content": prompt}])[0]["content"]
    assert kept.startswith("开头的规则") and kept.endswith("查证员的回答：3 日开")
    assert len(kept) < 60100
    assert compact[3]["tool_calls"] == [{"id": "c1"}]


@pytest.fixture
def traced_router() -> tuple[MessageRouter, Tracer, list[str]]:
    cfg = load_config()
    host = PluginHost(cfg)
    tracer = Tracer()
    host.services["tracer"] = tracer
    seen: list[str] = []

    async def ping(message: InboundMessage, args: str) -> OutboundMessage:
        return OutboundMessage(content=f"pong {args}".strip(), reply_to=message.message_id)

    async def observe(message: InboundMessage) -> None:
        seen.append(message.content)

    host.commands["ping"] = CommandSpec(name="ping", handler=ping, plugin_id="demo")
    host.observers.append(observe)
    router = MessageRouter(host, {"rate_limit": {"per_user_per_minute": 3}})
    return router, tracer, seen


async def test_router_records_route_and_outcome(
    traced_router: tuple[MessageRouter, Tracer, list[str]],
) -> None:
    router, tracer, seen = traced_router

    async def chat(message: InboundMessage) -> OutboundMessage | None:
        if "炸" in message.content:
            raise RuntimeError("对话入口炸了")
        await message.progress("正在查…")
        return OutboundMessage(content="回答", reply_to=message.message_id)

    router.set_chat_handler(chat)
    progress: list[OutboundMessage] = []

    async def sink(outbound: OutboundMessage) -> None:
        progress.append(outbound)

    out = await router.handle(_message("/ping 一下", message_id="a"))
    assert out is not None and out.content == "pong 一下"
    asked = _message("你好", message_id="b")
    asked.progress_sink = sink
    assert (await router.handle(asked)) is not None and len(progress) == 1
    with pytest.raises(RuntimeError):
        await router.handle(_message("炸了吗", message_id="c"))
    assert await router.handle(_message("超限了", message_id="d")) is not None  # 第 4 条：提示一次
    assert await router.handle(_message("还在超限", message_id="e")) is None  # 之后静默
    # 没 @ 机器人的群消息：旁听的看得到，但不留处理记录
    assert await router.handle(_message("路过", message_id="f", addressed=False)) is None
    # 网页对话里发的：照常处理、留记录，但不给旁听的插件看
    web = _message("/ping web", message_id="g", user_id="web-admin", chat_id="web-admin")
    web.channel = CHANNEL_WEB
    assert (await router.handle(web)) is not None

    rows = [(t.message_id, t.route, t.command, t.status, t.channel) for t in tracer.recent]
    assert rows == [
        ("a", "command", "ping", trace.REPLIED, "qq"),
        ("b", "chat", "", trace.REPLIED, "qq"),
        ("c", "chat", "", trace.ERROR, "qq"),
        ("d", "limited", "", trace.REPLIED, "qq"),
        ("e", "limited", "", trace.SILENT, "qq"),
        ("g", "command", "ping", trace.REPLIED, "web"),
    ]
    command, chatted, failed = list(tracer.recent)[:3]
    assert [(s.kind, s.title) for s in command.steps] == [("command", "/ping")]
    assert command.steps[0].data == {"plugin": "demo", "args": "一下"}
    assert [(s.kind, s.title) for s in chatted.steps] == [("progress", "正在查…")]
    assert "对话入口炸了" in failed.error and failed.seconds >= 0
    assert seen == ["/ping 一下", "你好", "炸了吗", "超限了", "还在超限", "路过"]
    assert trace.current() is None and tracer.active == {}
