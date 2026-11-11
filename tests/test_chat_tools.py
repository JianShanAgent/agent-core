"""chat 插件：工具循环、把问题交给查证工作流、去掉假链接。

查证工作流用的是真的（ResearchWorkflow），只把模型和取证来源换成按剧本走的替身；
带图片的消息另用一个只记参数的替身（FakeResearch），不依赖工作流内部怎么处理图片。
"""

import asyncio
import json
import re
from collections.abc import Collection, Sequence
from io import BytesIO
from types import SimpleNamespace
from typing import Any

from PIL import Image

from deployment import FULL_CONFIG_DIR
from jianshanagent.core.config import AppConfig, load_config
from jianshanagent.core.conversation import MemoryConversationStore
from jianshanagent.core.links import strip_unknown_links, urls_in, urls_in_result
from jianshanagent.core.llm import LLMError, LLMResult, LLMUsage, ToolCall
from jianshanagent.core.media import MediaError
from jianshanagent.core.messages import ChatScope, ImageRef, InboundMessage, OutboundMessage
from jianshanagent.core.plugin import PluginContext, erase_user_data
from jianshanagent.plugins.chat.plugin import ChatPlugin, citations_footer
from jianshanagent.plugins.research.evidence import Evidence
from jianshanagent.plugins.research.workflow import (
    Decision,
    Outcome,
    ResearchWorkflow,
    Verdict,
    WorkflowSettings,
)

REAL = "https://ds.163.com/feed/1/"
FAKE = "https://fake.example/x"
INLINE = "https://ds.163.com/feed/2/"  # 只出现在资料正文里的链接，也算查到过的
EVIDENCE = [
    Evidence(origin="知识库", title="水豚公告", url=REAL, text="3 日开", key="a"),
    Evidence(origin="知识库", title="别的", url="", text=f"无关，原帖 {INLINE}", key="b"),
]
PROGRESS = {"kb": "正在查知识库…", "review": "正在复核…"}

#: 对话模型的一次回复：（正文，工具调用），或者要抛出来的异常
Reply = tuple[str, list[ToolCall]] | Exception
#: 当成一次回复用：对话模型卡住不返回
HANG = TimeoutError("卡住")


class ScriptedLLM:
    """对话模型、查证员、复核员各按剧本回复，并记下收到的请求。剧本里的异常会被抛出来。"""

    def __init__(
        self,
        replies: Sequence[Reply],
        decisions: list[Any] | None = None,
        verdicts: list[Verdict] | None = None,
    ):
        self.settings = SimpleNamespace(history_max_turns=5, history_idle_seconds=7200)
        self.replies = list(replies)
        self.requests: list[dict[str, Any]] = []
        self.decisions = list(decisions or [])
        self.verdicts = list(verdicts or [])
        self.analyst_prompts: list[str] = []

    async def chat(self, messages: Any, *, profile: str, tools: Any = None) -> LLMResult:
        self.requests.append({"messages": list(messages), "tools": tools})
        reply = self.replies.pop(0)
        if reply is HANG:
            await asyncio.sleep(3600)
        if isinstance(reply, Exception):
            raise reply
        content, calls = reply
        message: dict[str, Any] = {"role": "assistant", "content": content}
        if calls:
            message["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
                }
                for call in calls
            ]
        return LLMResult(
            message=message,
            content=content,
            reasoning="",
            tool_calls=calls,
            finish_reason="tool_calls" if calls else "stop",
            usage=LLMUsage(),
            model="test",
        )

    async def chat_json(
        self, messages: Any, schema: Any, *, profile: str, attempts: int = 2
    ) -> Any:
        if schema is Verdict:
            return self.verdicts.pop(0) if self.verdicts else Verdict(ok=True)
        self.analyst_prompts.append(messages[0]["content"])
        item = self.decisions.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class KnowledgeBase:
    """只有一级的取证来源：不管问什么都给同样的资料。"""

    name, label, keywords_hint = "kb", "知识库", ""

    async def collect(self, question: str, keywords: str, have: Collection[str]) -> list[Evidence]:
        return EVIDENCE


def _setup(
    replies: Sequence[Reply],
    decisions: list[Any] | None = None,
    verdicts: list[Verdict] | None = None,
    services: dict[str, Any] | None = None,
    **overrides: float,
) -> tuple[Any, ScriptedLLM, MemoryConversationStore, list[str]]:
    """``decisions`` 不是 None 时带上查证工作流（查证员按它回复）；``services`` 是额外的服务。"""
    repo = load_config(FULL_CONFIG_DIR)
    chat_cfg = {**repo.plugin("chat"), **overrides}
    # 有查证工作流时仍然直接给模型的工具，是配置里点名的：这里拿站点插件会带来的筛选工具当例子
    chat_cfg["tools_with_research"] = [*chat_cfg["tools_with_research"], "wiki_filter"]
    config = AppConfig(root=repo.root, files={**repo.files, "plugin.chat": chat_cfg}, secrets={})
    llm = ScriptedLLM(replies, decisions, verdicts)
    store = MemoryConversationStore()
    services = {"llm": llm, "conversation_store": store, **(services or {})}
    if decisions is not None:
        services["research"] = ResearchWorkflow(
            llm,  # type: ignore[arg-type]
            [KnowledgeBase()],
            WorkflowSettings(progress=PROGRESS),
        )
    ctx = PluginContext("chat", config, services)
    executed: list[str] = []

    async def lookup(args: dict[str, Any], _message: InboundMessage) -> Any:
        executed.append(args["q"])
        return {"found": args["q"], "results": [{"title": "水豚公告", "url": REAL}]}

    ctx.add_tool("lookup", "查资料", {"type": "object", "properties": {}}, lookup)
    ctx.add_tool("wiki_filter", "筛选", {"type": "object", "properties": {}}, lookup)
    ChatPlugin().register(ctx)
    return ctx.chat_handler, llm, store, executed


def _call(number: int, name: str = "lookup") -> ToolCall:
    return ToolCall(id=f"c{number}", name=name, arguments={"q": f"q{number}"})


def _message(
    content: str = "问题", images: Sequence[str] = ()
) -> tuple[InboundMessage, list[OutboundMessage]]:
    """（一条消息，它中途发出去的进度消息）。``images`` 是消息里带的图片的地址。"""
    message = InboundMessage(
        message_id="m1",
        chat_scope=ChatScope.C2C,
        chat_id="c1",
        user_id="u1",
        content=content,
        images=[ImageRef(url=url) for url in images],
    )
    progress: list[OutboundMessage] = []

    async def sink(sent: OutboundMessage) -> None:
        progress.append(sent)

    message.progress_sink = sink
    return message, progress


ASK = ToolCall(id="r1", name="research", arguments={"context": "说的是水豚活动"})


def _names(request: dict[str, Any]) -> list[str]:
    return [tool["function"]["name"] for tool in request["tools"]]


# ---------- 没有查证工作流：模型自己用工具查 ----------


async def test_without_research_the_model_uses_every_tool_and_fake_links_are_dropped() -> None:
    answer = f"3 日开，见 [公告]({REAL}) 和 [攻略]({FAKE})"
    handler, llm, store, executed = _setup([("", [_call(1)]), (answer, [])], max_tool_rounds=4)
    message, progress = _message()
    reply = await handler(message)

    assert reply is not None and reply.content == f"3 日开，见 [公告]({REAL}) 和 攻略"
    assert executed == ["q1"] and _names(llm.requests[0]) == ["lookup", "wiki_filter"]
    assert progress == []
    history = await store.history("c2c:c1")
    assert history[-1]["content"] == reply.content  # 历史里存的也是去掉假链接后的


async def test_only_first_few_calls_of_a_round_are_executed() -> None:
    handler, llm, _store, executed = _setup(
        [("", [_call(n) for n in range(1, 6)]), ("答案", [])], max_tool_calls_per_round=2
    )
    reply = await handler(_message()[0])

    assert reply is not None and reply.content == "答案"
    assert executed == ["q1", "q2"]
    # 每个调用都要有对应的结果消息（接口要求），没执行的回一条说明
    results = [m for m in llm.requests[1]["messages"] if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in results] == ["c1", "c2", "c3", "c4", "c5"]
    assert json.loads(results[0]["content"])["found"] == "q1"
    assert all("没有执行" in json.loads(m["content"])["error"] for m in results[2:])


# ---------- 有查证工作流：模型只判断要不要查 ----------


def test_references_without_a_link_are_listed_by_title() -> None:
    # 网上搜来的网页没有能发出去的链接（平台只收报备过的域名）：只列标题
    items = [
        SimpleNamespace(title="水豚公告", url=REAL),
        SimpleNamespace(title="攻略（九游网）", url=""),
    ]
    assert citations_footer(items, markdown=True) == (
        f"\n\n参考资料：\n- [水豚公告]({REAL})\n- 攻略（九游网）"
    )
    assert citations_footer(items, markdown=False) == (
        f"\n\n参考资料：\n- 水豚公告 {REAL}\n- 攻略（九游网）"
    )


async def test_research_conclusion_goes_straight_to_the_user_with_progress_on_the_way() -> None:
    draft = f"水豚活动 3 日开，见 [公告]({REAL})、[原帖]({INLINE}) 和 [攻略]({FAKE})"
    # 模型同时发了两个 research：只查第一个
    again = ToolCall(id="r2", name="research", arguments={})
    handler, llm, store, executed = _setup(
        [("", [ASK, again])], [Decision(answer=draft, sources=[1])], [Verdict(ok=True, sources=[1])]
    )
    message, progress = _message()
    reply = await handler(message)

    assert reply is not None and reply.markdown is True
    # 结论（没查到过的链接只留文字）+ 参考资料；对话模型没有再被调用
    checked = f"水豚活动 3 日开，见 [公告]({REAL})、[原帖]({INLINE}) 和 攻略"
    assert reply.content == f"{checked}\n\n参考资料：\n- [水豚公告]({REAL})"
    assert len(llm.requests) == 1
    # 每到一个阶段发一句进度：单独的纯文本消息，回复的是用户那条
    assert [(p.content, p.reply_to, p.markdown) for p in progress] == [
        ("正在查知识库…", "m1", False),
        ("正在复核…", "m1", False),
    ]
    # 用户的原话原样交给工作流，模型只补上指代；回答的口吻和排版按对话的要求
    prompt = llm.analyst_prompts[0]
    assert "用户的问题：\n问题（说的是水豚活动）\n" in prompt and "8. 排版：" in prompt
    # 给模型的工具：research + 配置里点名的；没点名的（lookup）不给也不执行
    assert _names(llm.requests[0]) == ["wiki_filter", "research"] and executed == []

    history = await store.history("c2c:c1")
    assert [m["role"] for m in history] == ["user", "assistant", "tool", "tool", "assistant"]
    assert json.loads(history[2]["content"])["sources"] == [{"title": "水豚公告", "url": REAL}]
    assert "没有执行" in history[3]["content"]
    assert history[-1]["content"] == checked  # 历史里不带参考资料
    # 之后闲聊里再提到查到过的链接，不会被当成假链接
    assert REAL.rstrip("/") in urls_in(history[2]["content"])


async def test_research_without_a_conclusion_says_so_and_keeps_history_clean() -> None:
    # 查证员交了白卷
    handler, _llm, store, _ = _setup([("", [ASK])], [Decision(answer="")])
    message, progress = _message()
    reply = await handler(message)
    assert reply is not None and "没查到确切的信息" in reply.content
    assert [p.content for p in progress] == ["正在查知识库…"]
    assert await store.history("c2c:c1") == []

    # 查证员的接口出错
    handler, _llm, store, _ = _setup([("", [ASK])], [LLMError("挂了")])
    reply = await handler(_message()[0])
    assert reply is not None and "有点忙" in reply.content
    assert await store.history("c2c:c1") == []


async def test_empty_entry_reply_is_handed_to_research() -> None:
    # 对话模型把额度全花在思考上、什么都没给出来：不回「没查明白」，直接查证
    handler, llm, store, _ = _setup([("", [])], [Decision(answer="3 日开", sources=[1])])
    reply = await handler(_message()[0])
    assert reply is not None
    assert reply.content == f"3 日开\n\n参考资料：\n- [水豚公告]({REAL})"
    assert "用户的问题：\n问题\n" in llm.analyst_prompts[0]
    assert [m["role"] for m in await store.history("c2c:c1")] == ["user", "assistant"]


async def test_entry_model_that_hangs_is_bypassed_when_research_is_available() -> None:
    # 对话模型卡住不返回：不干等，直接交给查证
    handler, llm, _store, _ = _setup(
        [HANG], [Decision(answer="3 日开", sources=[1])], entry_timeout_seconds=0.01
    )
    reply = await handler(_message()[0])
    assert reply is not None and reply.content.startswith("3 日开")
    assert "用户的问题：\n问题\n" in llm.analyst_prompts[0]

    # 没有查证工作流可交：回「有点忙」
    handler, _llm, store, _ = _setup([HANG], entry_timeout_seconds=0.01)
    reply = await handler(_message()[0])
    assert reply is not None and "有点忙" in reply.content
    assert await store.history("c2c:c1") == []


async def test_chitchat_is_answered_directly_even_with_research() -> None:
    handler, llm, _store, _ = _setup([(f"你好呀，看看[这个]({FAKE})", [])], [])
    message, progress = _message()
    reply = await handler(message)
    assert reply is not None and reply.content == "你好呀，看看这个"
    assert progress == [] and llm.analyst_prompts == []
    system = llm.requests[0]["messages"][0]["content"]
    assert "research 工具" in system
    assert re.search(r"现在是 \d{4}-\d\d-\d\d 星期. \d\d:\d\d。", system)  # 模型知道现在几点


async def test_deleting_a_users_data_clears_the_history_and_the_turn_that_asked_is_not_kept() -> (
    None
):
    repo = load_config(FULL_CONFIG_DIR)
    forget = ToolCall(id="f1", name="forget", arguments={})
    llm = ScriptedLLM([("", [forget]), ("都删掉了", []), ("你好", [])])
    store = MemoryConversationStore()
    config = AppConfig(root=repo.root, files=repo.files, secrets={})
    ctx = PluginContext("chat", config, {"llm": llm, "conversation_store": store})

    async def forget_all(_args: dict[str, Any], message: InboundMessage) -> Any:
        deleted, _failed = await erase_user_data(ctx.erasers, message)
        return {"deleted": deleted}

    ctx.add_tool("forget", "删除用户的全部数据", {"type": "object", "properties": {}}, forget_all)
    ChatPlugin().register(ctx)
    earlier = [{"role": "user", "content": "我住在杭州"}, {"role": "assistant", "content": "好的"}]
    other = InboundMessage(
        message_id="m9", chat_scope=ChatScope.C2C, chat_id="c2", user_id="u2", content="x"
    )
    await store.append("c2c:c1", earlier)
    await store.append(other.session_key, earlier)

    # /清除记忆 走的就是这份清单：对话历史也是用户说过的话，一并删；别人的不动
    assert await erase_user_data(ctx.erasers, other) == ({"对话历史": 2}, [])
    assert await store.history(other.session_key) == []
    assert await store.history("c2c:c1") == earlier

    # 用一句话要求删除（模型调了删除的工具）：之前的删掉，这一轮自己也不再存进去——
    # 不然「把我的信息都删掉」这句话和上文又留在库里了
    reply = await ctx.chat_handler(_message("把我的信息都删掉")[0])
    assert reply is not None and reply.content == "都删掉了"
    assert json.loads(llm.requests[1]["messages"][-1]["content"]) == {"deleted": {"对话历史": 2}}
    assert await store.history("c2c:c1") == []
    # 之后的对话照常存
    await ctx.chat_handler(_message("嗨")[0])
    assert [m["content"] for m in await store.history("c2c:c1")] == ["嗨", "你好"]


# ---------- 带图片的消息 ----------


def _png(shade: int) -> bytes:
    out = BytesIO()
    Image.new("RGB", (8, 8), (shade, 30, 30)).save(out, format="PNG")
    return out.getvalue()


FIRST, SECOND = _png(200), _png(100)
PICTURES = {"https://qq.example/1": FIRST, "https://qq.example/2": SECOND}


class FakeMedia:
    """图片下载的替身：登记过的地址给字节，没登记的当成下载失败。"""

    def __init__(self, files: dict[str, bytes]):
        self.files = files

    async def fetch(self, url: str) -> bytes:
        if url not in self.files:
            raise MediaError("HTTP 400")
        return self.files[url]


class FakeResearch:
    """查证工作流的替身：记下收到的问题和额外的参数，直接给结论。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def run(
        self, question: str, *, style: str = "", on_progress: Any = None, **extra: Any
    ) -> Outcome:
        self.calls.append({"question": question, **extra})
        return Outcome(status="answered", answer="是「花合战」皮肤")


def _with_images(
    replies: Sequence[Reply], files: dict[str, bytes] = PICTURES
) -> tuple[Any, ScriptedLLM, MemoryConversationStore, FakeResearch]:
    research = FakeResearch()
    handler, llm, store, _ = _setup(
        replies, services={"research": research, "media": FakeMedia(files)}
    )
    return handler, llm, store, research


async def test_images_are_shown_to_the_model_and_the_originals_go_to_research() -> None:
    describe = ToolCall(id="r1", name="research", arguments={"context": "红色旗袍，白色狐尾"})
    handler, llm, store, research = _with_images([("", [describe])])
    urls = [*PICTURES, "https://qq.example/3"]
    reply = await handler(_message("这是哪个皮肤", images=urls)[0])

    assert reply is not None and reply.content == "是「花合战」皮肤"
    system, user = llm.requests[0]["messages"]
    # 模型看到的是缩小成 JPEG 的图。一条消息最多看 2 张，第 3 张没看，文字里说了
    *pictures, text = user["content"]
    assert [p["image_url"]["url"][:23] for p in pictures] == ["data:image/jpeg;base64,"] * 2
    assert text["text"].startswith("这是哪个皮肤（另有 1 张图片没有给你看")
    assert "以图搜图" in system["content"]
    # 交给查证的是原图；问题还是「用户原话（模型补的说明）」
    assert research.calls == [
        {"question": "这是哪个皮肤（红色旗袍，白色狐尾）", "images": [FIRST, SECOND]}
    ]
    # 历史里只留占位，不存图片
    history = await store.history("c2c:c1")
    assert history[0]["content"].startswith("[图片][图片]这是哪个皮肤")

    # 只发了图片没说话，对话模型又什么都没给出来：照样带着原图交给查证
    handler, llm, _store, research = _with_images([("", [])])
    await handler(_message("", images=["https://qq.example/1"])[0])
    assert llm.requests[0]["messages"][1]["content"][1]["text"] == "（用户只发了图片，没有说话）"
    assert research.calls == [{"question": "（用户只发了图片，没有说话）", "images": [FIRST]}]


async def test_images_that_cannot_be_fetched_are_mentioned_and_left_out() -> None:
    # 一张下载失败，一张下下来不是图片
    handler, llm, _store, research = _with_images(
        [("", [ASK])], {"https://qq.example/broken": b"not an image"}
    )
    urls = ["https://qq.example/gone", "https://qq.example/broken"]
    reply = await handler(_message("这是谁", images=urls)[0])

    assert reply is not None and reply.content == "是「花合战」皮肤"  # 这条消息照常处理
    system, user = llm.requests[0]["messages"]
    assert user["content"].startswith("这是谁（用户发的图片里有 2 张没能取到")
    assert "以图搜图" not in system["content"]
    assert research.calls == [{"question": "这是谁（说的是水豚活动）"}]  # 没有图就不带 images


async def test_model_that_refuses_images_is_asked_again_without_them() -> None:
    refusal = LLMError("LLM 接口返回 HTTP 400：Param Incorrect", status=400)
    handler, llm, _store, research = _with_images([refusal, ("", [ASK])])
    reply = await handler(_message("这是哪个皮肤", images=["https://qq.example/1"])[0])

    assert reply is not None and reply.content == "是「花合战」皮肤"
    first, second = (request["messages"][1]["content"] for request in llm.requests)
    assert first[0]["type"] == "image_url"
    assert second == "这是哪个皮肤（用户发了 1 张图片，你看不到，图片已交给 research 识别）"
    assert research.calls[0]["images"] == [FIRST]  # 模型看不了，原图照样交给查证

    # 接口本身出了故障（不是嫌请求不对）：不重试，照旧说忙
    handler, llm, _store, research = _with_images([LLMError("挂了", status=503)])
    reply = await handler(_message("这是哪个皮肤", images=["https://qq.example/1"])[0])
    assert reply is not None and "有点忙" in reply.content
    assert len(llm.requests) == 1 and research.calls == []


async def test_without_research_the_model_still_sees_the_image() -> None:
    handler, llm, store, _ = _setup(
        [("一只白色的狐狸", [])], services={"media": FakeMedia(PICTURES)}
    )
    reply = await handler(_message("图里有什么", images=["https://qq.example/1"])[0])

    assert reply is not None and reply.content == "一只白色的狐狸"
    picture, text = llm.requests[0]["messages"][1]["content"]
    assert picture["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert text == {"type": "text", "text": "图里有什么"}
    assert (await store.history("c2c:c1"))[0]["content"] == "[图片]图里有什么"


# ---------- 链接核对 ----------


def test_links_match_regardless_of_encoding_and_anchor() -> None:
    real = "https://w.test/wiki/%E8%8B%8D%E9%A3%8E"
    known = urls_in_result({"results": [{"title": "苍风一目连", "url": real, "x": [{"url": 1}]}]})
    assert known == {"https://w.test/wiki/苍风"}
    text = f"见 [改动](https://w.test/wiki/苍风#技能改动历史)、{real} 和 https://w.test/wiki/别的。"
    assert strip_unknown_links(text, known) == (
        f"见 [改动](https://w.test/wiki/苍风#技能改动历史)、{real} 和 。"
    )
    assert urls_in('{"url": "https://w.test/a"}') == {"https://w.test/a"}  # JSON 里的也认得出
