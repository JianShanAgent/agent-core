"""闲聊与自然语言查询的入口：人设 + 多轮历史 + 判断要不要查资料。

没有命中任何命令的消息会落到这里。

- 有查证工作流（research 插件）时，对话模型只负责判断这句话要不要查：闲聊直接回；
  涉及 wiki 上的内容就调用 research 工具把问题交给工作流。工作流的结论直接发给用户，
  不再经过对话模型（复核过的结论不能再被改写），后面附参考资料。
  查证要花二三十秒，工作流每到一个阶段，就给用户发一句进度（``InboundMessage.progress``）。
- 没有查证工作流（没配数据库）时退回老办法：模型自己用其它插件注册的工具查，再作答。

用户发来的图片（``InboundMessage.images``）在这里下载：缩小后给对话模型看，原图交给查证工作流
做以图搜图。取不到的图当成没有，在文字里告诉模型；模型的接口不收图片时，去掉图片再问一次。
存进历史的是「[图片]」这个占位，不是图片本身（见 core/conversation.py）。

回复里出现、但没有查到过的链接一律去掉（模型会编假链接），这一步是程序核对的，见 core/links.py。

「什么问题该查」因站而异（阴阳师 wiki 是式神、御魂、活动……），所以那几段话不写死在代码里：
``plugin.chat`` 的 ``prompts`` 段，没写的用这里对什么 wiki 都说得通的说法
（``DEFAULT_PROMPTS``），站点插件在自己配置的 ``extend`` 段里换成那个站的说法。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Sequence
from typing import Any
from zoneinfo import ZoneInfo

from jianshanagent.core import trace
from jianshanagent.core.clock import now_text
from jianshanagent.core.conversation import ConversationStore, MemoryConversationStore
from jianshanagent.core.images import jpeg_data_uri, normalize_image
from jianshanagent.core.links import normalize_url, strip_unknown_links, urls_in, urls_in_result
from jianshanagent.core.llm import (
    LLMClient,
    LLMContentBlocked,
    LLMError,
    LLMResult,
    Message,
    ToolCall,
    function_tool,
    tool_result_message,
    user_message,
)
from jianshanagent.core.logging import get_logger
from jianshanagent.core.media import DEFAULT_MAX_BYTES, DEFAULT_TIMEOUT_SECONDS, MediaFetcher
from jianshanagent.core.messages import ImageRef, InboundMessage, OutboundMessage
from jianshanagent.core.plugin import PluginContext, ToolSpec

logger = get_logger(__name__)

_OMITTED = "…（后面的已省略，需要细节请重新查询）"
_STYLE_TEXT = "用纯文本，不要用 Markdown；简洁，适合在 QQ 里阅读。"
_STYLE_MARKDOWN = (
    "简洁，适合在 QQ 里阅读。可以用简单的 Markdown：**加粗**、列表、"
    "[文字](链接)；不要用表格、代码块和多级标题。"
)
#: 告诉对话模型「什么问题该查、怎么查」的几段话，没在配置里另写时用这些（对什么 wiki 都说得通）
DEFAULT_PROMPTS = {
    # 没有查证工作流时：模型自己用工具查
    "facts": (
        "涉及 wiki 收录的内容必须先用工具查 wiki，以工具结果为准，"
        "不要凭记忆回答；工具查不到就直说没查到。"
    ),
    # 有查证工作流时：模型只判断要不要查
    "facts_research": (
        "涉及 wiki 收录的内容的问题，"
        "一律立刻调用 research 工具：你不需要分析问题、不需要自己想答案，也不要猜题意、"
        "不要凭记忆回答、不要自己写链接。拿不准是不是在问 wiki 上的内容时"
        "（出现了你不认识的名字、外号、简称），"
        "也调用 research，不要反问用户。闲聊、打招呼、问你是谁，直接回复。"
    ),
    # 有查证工作流、而且这条消息有图片交得过去时才加这一条
    "images_research": (
        "用户这条消息带了图片。如果是在问图里是什么、是谁、出自哪里，或者只发了图片没有说话："
        "调用 research，图片会一并交给它做以图搜图；你自己不要凭印象认。"
        "这时 context 里只写图片上看得见的文字和客观内容（颜色、物件、界面上的字），不要猜名字。"
    ),
    # research 工具的说明
    "research_description": (
        "查证 wiki 相关的问题，并把结论直接回复给用户。凡是涉及 wiki 收录的内容的问题"
        "都必须调用它，不要凭记忆回答。用户的原话会原样交给它，它依次查 wiki 知识库、站内搜索"
        "（必要时联网），核对无误后把结论发给用户；调用以后你不用再回复。一条消息只调用一次。"
    ),
}
_IMAGE_ONLY = "（用户只发了图片，没有说话）"

RESEARCH_TOOL = "research"
_RESEARCH_PARAMETERS = {
    "type": "object",
    "properties": {
        "context": {
            "type": "string",
            "description": (
                "用户这句话离开上文看不懂时（有「它」「那个活动」「上面说的」这类指代，"
                "或者省略了说的是谁），用一句话补上缺的信息，例如「说的是千姬」；"
                "这句话本身已经完整就留空。不要改写、扩写或解读用户的问题"
            ),
        }
    },
}
_NOT_FOUND = "这个问题我没查到确切的信息，换个问法或说得具体一点试试。"
_BUSY = "我这会儿有点忙，稍后再试试。"
_BLOCKED = "这个问题我没法回答，换个问法试试吧。"


def _refused(exc: LLMError) -> bool:
    """接口嫌请求本身不对（4xx）。带着图片的请求遇到这种拒绝，就去掉图片再问一次。

    鉴权、审核拦截、限流不算：那些和带没带图片无关。实测给的不是图片时返回的是 400。
    """
    return (
        exc.status is not None
        and 400 <= exc.status < 500
        and exc.status not in (401, 403, 421, 429)
    )


def shorten_tool_results(history: list[Message], max_chars: int) -> list[Message]:
    """把历史里过长的工具结果截短（``max_chars`` 为 0 时不动）。不改传进来的消息。"""
    if max_chars <= 0:
        return history
    out: list[Message] = []
    for message in history:
        content = message.get("content")
        if message.get("role") == "tool" and isinstance(content, str) and len(content) > max_chars:
            message = {**message, "content": content[:max_chars] + _OMITTED}
        out.append(message)
    return out


def citations_footer(items: Sequence[Any], *, markdown: bool) -> str:
    """回答后面的参考资料。``items`` 是带 ``title`` 和 ``url`` 的资料；
    没有链接的（网上搜来的网页）只列标题。部署挂了不止一个 wiki 时资料上带着出处
    （``source``），标在标题后面。"""
    if not items:
        return ""
    lines: list[str] = []
    for item in items:
        source = getattr(item, "source", "")
        tail = f"（{source}）" if source else ""
        if not item.url:
            lines.append(f"- {item.title}{tail}")
        elif markdown:
            label = (item.title or item.url).replace("[", "【").replace("]", "】")
            url = item.url.replace("(", "%28").replace(")", "%29")
            lines.append(f"- [{label}]({url}){tail}")
        else:
            lines.append(f"- {item.title}{tail} {item.url}".replace("  ", " "))
    return "\n\n参考资料：\n" + "\n".join(lines)


class ChatPlugin:
    id = "chat"

    def register(self, ctx: PluginContext) -> None:
        cfg = ctx.plugin_config()
        if not cfg.get("enabled", False):
            logger.info("chat_plugin_disabled")
            return
        llm: LLMClient | None = ctx.services.get("llm")
        if llm is None:
            logger.warning("chat_plugin_needs_llm", hint="core.llm.yaml 的 enabled 为 false")
            return

        # 有数据库时由 main 注册落库的实现；没有就放内存（重启后历史清空）
        store: ConversationStore = (
            ctx.services.get("conversation_store") or MemoryConversationStore()
        )
        persona = ctx.config.get(f"persona/{cfg.get('persona') or 'persona.default'}")
        persona_prompt = str(persona.get("system_prompt") or "").strip()
        written = cfg.get("prompts") or {}
        # 一段话在配置里可以分几行写，拼给模型时并成一行
        prompts = {
            key: " ".join(str(written.get(key) or "").split()) or default
            for key, default in DEFAULT_PROMPTS.items()
        }
        timezone = ZoneInfo(str(ctx.config.app.get("timezone") or "Asia/Shanghai"))
        max_rounds = int(cfg.get("max_tool_rounds") or 4)
        entry_timeout = float(cfg.get("entry_timeout_seconds") or 0)
        max_calls = int(cfg.get("max_tool_calls_per_round") or 3)
        tool_result_chars = int(cfg.get("tool_result_chars") or 16000)
        history_tool_chars = int(cfg.get("history_tool_result_chars") or 0)
        with_research = cfg.get("tools_with_research")
        allowed = {str(name) for name in with_research} if with_research is not None else None
        tools = ctx.tools  # 共享的工具表，调用时再读，后加载的插件注册的工具也能用到
        markdown = bool(cfg.get("markdown", False))
        style = _STYLE_MARKDOWN if markdown else _STYLE_TEXT
        # 给查证工作流的：结论直接发给用户，所以口吻和排版要和对话模型自己回的一致
        research_style = f"排版：{style}口吻按机器人的人设来：{' '.join(persona_prompt.split())}"
        services = ctx.services
        images_cfg = cfg.get("images") or {}
        per_message = images_cfg.get("max_per_message")
        max_images = int(per_message) if per_message is not None else 2
        if services.get("media") is None:
            megabytes = float(images_cfg.get("max_megabytes") or 0)
            seconds = float(images_cfg.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS)
            # 注册成服务：进程退出时 main 会把它的连接关掉
            ctx.register_service(
                "media",
                MediaFetcher(
                    max_bytes=int(megabytes * 1024 * 1024) or DEFAULT_MAX_BYTES,
                    timeout_seconds=seconds,
                ),
            )
        fetcher: MediaFetcher = services["media"]

        async def load_image(ref: ImageRef) -> tuple[bytes, str] | None:
            """下载一张图 →（原图，缩小后给模型看的 data URI）。取不到返回 None。"""
            try:
                # 管理后台网页对话里上传的图片直接带着内容，不用下载
                original = ref.data or await fetcher.fetch(ref.url)
                # 解码、缩放比较费 CPU，放到线程里做，不耽误别的消息
                jpeg = await asyncio.to_thread(normalize_image, original)
            except Exception as exc:
                # 图片是用户给的，什么样的坏文件都可能有：下不来、认不出都当成没有这张图，
                # 不能因为一张图让整条消息失败
                logger.warning("chat_image_unavailable", error=f"{type(exc).__name__}: {exc}"[:300])
                return None
            return original, jpeg_data_uri(jpeg)

        async def load_images(message: InboundMessage) -> tuple[list[bytes], list[str], str]:
            """取这条消息带的图片 →（原图，给模型看的 data URI，要告诉模型的说明）。

            只取前 ``max_images`` 张。有图没取到或者没取时，说明里写清楚，补在用户的话后面。
            """
            refs = message.images[:max_images]
            results = await asyncio.gather(*map(load_image, refs))
            loaded = [item for item in results if item is not None]
            if message.images:
                logger.info("chat_images", attached=len(message.images), loaded=len(loaded))
            notice = ""
            if missing := len(refs) - len(loaded):
                notice += (
                    f"（用户发的图片里有 {missing} 张没能取到，你看不到；"
                    "需要看图才能回答的话，请用户重新发一次）"
                )
            if skipped := len(message.images) - len(refs):
                notice += f"（另有 {skipped} 张图片没有给你看：一条消息最多看 {max_images} 张）"
            return [original for original, _ in loaded], [uri for _, uri in loaded], notice

        async def system_message(message: InboundMessage, research: bool, images: bool) -> Message:
            parts = [
                persona_prompt,
                f"现在是 {now_text(timezone)}。",
                f"回复规则：{style}",
                prompts["facts_research"] if research else prompts["facts"],
            ]
            if research and images:
                parts.append(prompts["images_research"])
            memory = services.get("memory")  # memory 插件启用时才有
            if memory is not None:
                parts.append(await memory.prompt(message))
            return {"role": "system", "content": "\n".join(parts)}

        def offered_tools(research: bool) -> list[ToolSpec]:
            """给对话模型哪些工具。有查证工作流时只留配置里点名的（其余的活都归工作流）。"""
            if research and allowed is not None:
                return [tool for tool in tools.values() if tool.name in allowed]
            return list(tools.values())

        async def run_tool(
            call: ToolCall, message: InboundMessage, offered: dict[str, ToolSpec]
        ) -> tuple[str, set[str]]:
            """（给模型看的结果，结果里出现的链接）。"""
            spec = offered.get(call.name)
            with trace.span("tool", call.name, arguments=call.arguments) as record:
                if spec is None:
                    result: object = {"error": f"没有这个工具：{call.name}"}
                elif not call.valid:
                    result = {"error": "参数不是合法的 JSON 对象"}
                else:
                    try:
                        result = await spec.handler(call.arguments, message)
                    except Exception as exc:  # 工具出错要回给模型，让它换个办法或如实告知用户
                        logger.exception("chat_tool_failed", tool=call.name)
                        result = {"error": f"工具执行失败：{exc}"}
                        record.ok = False
                text = json.dumps(result, ensure_ascii=False)[:tool_result_chars]
                record.set(chars=len(text), result=trace.brief(text, 600))
            return text, urls_in_result(result)

        def note(text: str) -> str:
            return json.dumps({"error": text}, ensure_ascii=False)

        #: 正在处理的消息 → 处理过程中用户的数据是不是被删过
        #: （他用一句话要求删除，模型调了删除的工具）
        forgetting: dict[str, bool] = {}

        async def erase_history(message: InboundMessage) -> int:
            """用户要求删除他的数据（/清除记忆）：对话历史也是他说过的话，一并删。返回删了几条消息。"""
            if message.message_id in forgetting:
                forgetting[message.message_id] = True  # 正在处理的这一轮也不要存
            return await store.clear(message.session_key, user_suffix=message.user_suffix)

        ctx.add_eraser("对话历史", erase_history)

        async def remember(message: InboundMessage, turn: list[Message]) -> None:
            """把这一轮存进对话历史；处理过程中用户刚要求过删除他的数据就不存。"""
            if not forgetting.get(message.message_id):
                await store.append(message.session_key, turn)

        async def handle(message: InboundMessage) -> OutboundMessage | None:
            forgetting[message.message_id] = False
            try:
                return await respond(message)
            finally:
                forgetting.pop(message.message_id, None)

        async def respond(message: InboundMessage) -> OutboundMessage | None:
            history = await store.history(
                message.session_key,
                max_turns=llm.settings.history_max_turns,
                idle_seconds=llm.settings.history_idle_seconds,
            )
            history = shorten_tool_results(history, history_tool_chars)
            research: Any = services.get("research")  # research 插件启用且可用时才有
            offered = {tool.name: tool for tool in offered_tools(research is not None)}
            specs = [function_tool(t.name, t.description, t.parameters) for t in offered.values()]
            if research is not None:
                specs.append(
                    function_tool(
                        RESEARCH_TOOL, prompts["research_description"], _RESEARCH_PARAMETERS
                    )
                )
            # 用户的原话；只发了图片没说话时用一句说明顶上。给模型看的、交给查证的都从它来
            asked = message.content or _IMAGE_ONLY
            originals, shown, image_note = await load_images(message)
            turn: list[Message] = [user_message(asked + image_note, images=shown)]
            # 查到过的链接：之前对话里出现过的 + 这一轮工具结果里的；其余的都是模型编的
            known: set[str] = set()
            for past in history:
                if isinstance(past.get("content"), str):
                    known |= urls_in(past["content"])

            def reply(body: str) -> OutboundMessage:
                return OutboundMessage(content=body, reply_to=message.message_id)

            async def ask(system: Message) -> LLMResult:
                """问对话模型。带着图片被接口拒绝时，去掉图片、在文字里说明，再问一次。"""
                nonlocal shown

                def chat() -> Awaitable[LLMResult]:
                    return llm.chat([system, *history, *turn], profile="chat", tools=specs or None)

                try:
                    return await chat()
                except LLMError as exc:
                    if not shown or not _refused(exc):
                        raise
                    logger.warning("chat_images_refused", status=exc.status, error=str(exc)[:300])
                handed = "，图片已交给 research 识别" if research is not None else ""
                turn[0] = user_message(
                    f"{asked}（用户发了 {len(message.images)} 张图片，你看不到{handed}）"
                )
                shown = []  # 这条消息后面几轮也不再带图
                return await chat()

            async def investigate(question: str) -> Any:
                """交给查证工作流。取到了图片就把原图一并交过去（它拿去以图搜图）。"""
                if originals:
                    return await research.run(
                        question,
                        style=research_style,
                        on_progress=message.progress,
                        images=originals,
                    )
                return await research.run(
                    question, style=research_style, on_progress=message.progress
                )

            answer: str | None = None
            outcome: Any = None
            try:
                for _ in range(max_rounds + 1):
                    # 每轮重新生成系统提示：上一轮刚记下的记忆马上就能看到
                    system = await system_message(message, research is not None, bool(originals))
                    try:
                        with trace.span("entry", "对话模型：直接回，还是去查") as record:
                            result = await asyncio.wait_for(ask(system), entry_timeout or None)
                            record.set(
                                calls=[call.name for call in result.tool_calls],
                                reply=trace.brief(result.content),
                            )
                    except TimeoutError:
                        # 模型偶尔会卡很久（实测有等一两分钟的）：
                        # 第一轮就卡住、又有查证工作流时，不等它判断了，直接交过去
                        if research is None or len(turn) > 1:
                            raise LLMError(f"对话模型 {entry_timeout:.0f} 秒没有回应") from None
                        logger.warning(
                            "chat_entry_timeout_handed_to_research", session=message.session_key
                        )
                        outcome = await investigate(asked)
                        break
                    turn.append(result.message)
                    if not result.tool_calls:
                        answer = result.content.strip()
                        break
                    for position, call in enumerate(result.tool_calls):
                        if research is not None and call.name == RESEARCH_TOOL:
                            if outcome is None:
                                # 用户的原话原样交给工作流（让模型转述，它会扩写、会加自己的猜测），
                                # 模型只补上离开上文看不懂的部分
                                context = str(call.arguments.get("context") or "").strip()
                                question = asked
                                if context:
                                    question = f"{question}（{context}）"
                                outcome = await investigate(question)
                                cited = [{"title": e.title, "url": e.url} for e in outcome.cited]
                                content = json.dumps(
                                    {"status": "已查证并回复用户", "sources": cited},
                                    ensure_ascii=False,
                                )
                            else:
                                content = note("一条消息只查证一次，这个没有执行")
                        elif position < max_calls:
                            # 模型偶尔会一口气发出上百个调用（实测出现过）：只执行前几个
                            content, found = await run_tool(call, message, offered)
                            known |= found
                        else:
                            content = note(f"一次最多调用 {max_calls} 个工具，这个没有执行")
                        turn.append(tool_result_message(call.id, content))
                    if outcome is not None:
                        break
                if research is not None and outcome is None and not answer and len(turn) == 2:
                    # 对话模型什么都没给出来（实测：它把思考的额度全花在琢磨问题上了）：直接交给查证
                    logger.warning(
                        "chat_entry_empty_handed_to_research", session=message.session_key
                    )
                    turn.pop()
                    outcome = await investigate(asked)
            except LLMContentBlocked:
                return reply(_BLOCKED)
            except LLMError:
                logger.exception("chat_llm_failed", session=message.session_key)
                return reply(_BUSY)

            if outcome is not None:
                if outcome.status != "answered":
                    # 没有结论：不存这一轮，避免污染历史
                    return reply(_NOT_FOUND if outcome.status == "not_found" else _BUSY)
                # 查到过的链接还包括：资料自己的链接，和资料正文里带的链接（原帖地址之类）
                for item in outcome.evidence:
                    known |= {normalize_url(item.url)} | urls_in(item.text)
                answer = strip_unknown_links(outcome.answer, known)
                turn.append({"role": "assistant", "content": answer})  # 历史里不带参考资料
                await remember(message, turn)
                out = reply(answer + citations_footer(outcome.cited, markdown=markdown))
                out.markdown = markdown
                return out

            if not answer:
                # 工具轮数用完还没答出来，或模型给了空回复：不存这一轮，避免污染历史
                return reply("这个问题我没查明白，换个问法或说得具体一点试试。")
            answer = strip_unknown_links(answer, known)
            turn[-1] = {**turn[-1], "content": answer}  # 历史里存去掉假链接后的回答
            await remember(message, turn)
            out = reply(answer)
            out.markdown = markdown
            return out

        async def handle_reset(message: InboundMessage, _args: str) -> OutboundMessage | None:
            # 只清对话历史；memory 插件记住的事不动（那个用 /清除记忆，它连对话历史一起删）
            await store.clear(message.session_key)
            return OutboundMessage(
                content="好的，之前聊的我不再参考，我们重新开始。", reply_to=message.message_id
            )

        reset_name = str((cfg.get("commands") or {}).get("reset") or "新话题")
        ctx.add_command(reset_name, handle_reset, description="清空和机器人的对话上下文，重新开始")
        ctx.set_chat_handler(handle)
        logger.info("chat_plugin_enabled", max_tool_rounds=max_rounds)


def create_plugin() -> ChatPlugin:
    return ChatPlugin()
