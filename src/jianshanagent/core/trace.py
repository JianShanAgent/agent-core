"""处理记录：一条对机器人说的消息从进到出，走了哪几步、各步多久、调了几次模型、用了多少 token。

管理后台的统计和「查证过程」都从这里来；以前这些只在日志里，要翻日志才知道一个问题为什么慢、
为什么答成这样。

用法分三处：

- 路由（core/router.py）在开始处理一条消息时 :meth:`Tracer.begin`，处理完 :meth:`Tracer.finish`。
- 处理过程中的代码（查证工作流、对话入口）用 :func:`span` 把一个阶段包起来，往里填这个阶段的结果；
  一闪而过的事用 :func:`event`；整条记录上的结论用 :func:`note`。当前这条消息的记录放在
  ``contextvars`` 里，调用的地方不用把它一层层传下去；不在处理消息时（跑测试、后台任务）
  这几个函数什么都不记，照常能用。
- 模型调用层（core/llm.py）每调一次模型交一份 :class:`LLMCall` 给 :meth:`Tracer.record_call`，
  它属于哪条消息、哪个阶段，从当时的上下文里认。

落到哪里由 :class:`TraceSink` 决定（数据库的实现在 core/trace_store.py）。存是在后台存的，
存不进去只记一条日志，不影响回复。
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import secrets
import time
from collections import deque
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from structlog.contextvars import bind_contextvars, unbind_contextvars

from jianshanagent.core.logging import get_logger
from jianshanagent.core.messages import InboundMessage, OutboundMessage

logger = get_logger(__name__)

#: 处理结果
REPLIED = "replied"  # 回复了
SILENT = "silent"  # 处理了，但没有要回的（被限流后的静默、中间件丢弃）
ERROR = "error"  # 处理时出错

_PREVIEW_CHARS = 300
#: 存进记录的一条消息正文最多多少字。查证员、复核员的提示词带着全部资料：挑中的页面给整页以后
#: 平均三四万字，大的十几万字。超出时留开头和结尾、去掉中间——结尾是这次要它做什么
#: （复核员提示词的最后是查证员的回答，修正的最后是原回答和复核意见），不能丢
_MESSAGE_CHARS = 60000
_MESSAGE_TAIL_CHARS = 20000
_MEDIA_PLACEHOLDERS = {"image_url": "[图片]", "input_audio": "[语音]", "video_url": "[视频]"}


def brief(value: Any, limit: int = _PREVIEW_CHARS) -> str:
    """长文字只留开头。"""
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "…"


@dataclass(slots=True)
class Step:
    """处理过程中的一步。``seconds`` 为 None 的是一闪而过的事（发了一句进度、路由到了哪里）。"""

    id: int
    kind: str
    title: str
    #: 这一步开始时，距这条消息开始处理过了多少秒
    at: float
    seconds: float | None = None
    ok: bool = True
    data: dict[str, Any] = field(default_factory=dict)

    def set(self, **data: Any) -> None:
        self.data.update(data)

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "at": round(self.at, 3),
            "seconds": None if self.seconds is None else round(self.seconds, 3),
            "ok": self.ok,
            "data": self.data,
        }


@dataclass(slots=True)
class LLMCall:
    """一次模型调用。"""

    started_at: datetime
    seconds: float
    profile: str
    model: str
    thinking: bool
    ok: bool
    finish_reason: str = ""
    error: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    cached_tokens: int = 0
    tool_calls: int = 0
    #: 哪个环节调的（所在那一步的 ``kind``）；不在任何一步里就是空的
    purpose: str = ""
    trace_id: str | None = None
    step_id: int | None = None
    #: 发给模型的内容和它的回复（图片换成了占位）；配置里关了「留提示词」时是 None
    request: dict[str, Any] | None = None
    response: dict[str, Any] | None = None


def compact_messages(messages: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """发给模型的消息 → 可以长期存的样子：图片等媒体换成占位，过长的正文截断。"""
    out: list[dict[str, Any]] = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, list):
            pieces: list[str] = []
            for part in content:
                kind = part.get("type") if isinstance(part, dict) else None
                if kind == "text":
                    pieces.append(str(part.get("text") or ""))
                elif kind in _MEDIA_PLACEHOLDERS:
                    pieces.append(_MEDIA_PLACEHOLDERS[kind])
            content = "".join(pieces)
        if isinstance(content, str) and len(content) > _MESSAGE_CHARS:
            head = _MESSAGE_CHARS - _MESSAGE_TAIL_CHARS
            dropped = len(content) - _MESSAGE_CHARS
            content = (
                content[:head]
                + f"\n…（共 {len(content)} 字，中间这 {dropped} 字没有存）…\n"
                + content[-_MESSAGE_TAIL_CHARS:]
            )
        out.append({**message, "content": content})
    return out


def _new_id() -> str:
    """按时间排得出先后的编号，例如 ``20261111-174501-a1b2c3``。"""
    return f"{datetime.now().astimezone():%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"


class Trace:
    """一条消息的处理记录。"""

    def __init__(self, message: InboundMessage):
        self.id = _new_id()
        self.started_at = datetime.now(UTC)
        self._clock = time.perf_counter()
        self.channel = message.channel
        self.scope = message.chat_scope.value
        self.chat_id = message.chat_id
        self.user_id = message.user_id
        self.user_name = message.user_name
        self.session_key = message.session_key
        self.message_id = message.message_id
        self.question = message.content
        self.images = len(message.images)
        #: 走了哪条路：command / chat / research / keyword / session / limited / dropped / none
        self.route = ""
        self.command = ""
        self.status = ""
        #: 查证的结果（answered / not_found / failed）；没走查证就是空的
        self.outcome = ""
        self.reply = ""
        self.error = ""
        self.seconds = 0.0
        self.steps: list[Step] = []
        self.calls: list[LLMCall] = []
        #: 整条记录上的补充信息（查证的概况等）
        self.summary: dict[str, Any] = {}
        #: 这条消息是用户要求删除他的数据（见 :func:`forget_current`）：这条记录不存
        self.forgotten = False
        self._tokens: tuple[contextvars.Token[Any], contextvars.Token[Any]] | None = None

    @property
    def elapsed(self) -> float:
        return time.perf_counter() - self._clock

    def open(self, kind: str, title: str, data: dict[str, Any], *, timed: bool) -> Step:
        step = Step(
            id=len(self.steps) + 1,
            kind=kind,
            title=title,
            at=self.elapsed,
            seconds=0.0 if timed else None,
            data=data,
        )
        self.steps.append(step)
        return step

    def to_json(self, *, finished: bool = True) -> dict[str, Any]:
        """给管理后台看的样子（和数据库里存的一致；还在处理的 ``finished`` 为假）。"""
        calls = self.calls
        return {
            "id": self.id,
            "started_at": self.started_at.isoformat(),
            "seconds": round(self.seconds if finished else self.elapsed, 3),
            "finished": finished,
            "channel": self.channel,
            "scope": self.scope,
            "chat_id": self.chat_id,
            "user_id": self.user_id,
            "user_name": self.user_name,
            "session_key": self.session_key,
            "message_id": self.message_id,
            "question": self.question,
            "images": self.images,
            "route": self.route,
            "command": self.command,
            "status": self.status,
            "outcome": self.outcome,
            "reply": self.reply,
            "error": self.error,
            "llm_calls": len(calls),
            "prompt_tokens": sum(call.prompt_tokens for call in calls),
            "completion_tokens": sum(call.completion_tokens for call in calls),
            "reasoning_tokens": sum(call.reasoning_tokens for call in calls),
            "steps": [step.to_json() for step in self.steps],
            "summary": self.summary,
        }


_trace: contextvars.ContextVar[Trace | None] = contextvars.ContextVar(
    "jianshanagent_trace", default=None
)
_step: contextvars.ContextVar[Step | None] = contextvars.ContextVar(
    "jianshanagent_step", default=None
)


#: 不在处理消息时发起的模型调用（管理后台里测试模型）算在哪个环节名下
_purpose: contextvars.ContextVar[str] = contextvars.ContextVar("jianshanagent_purpose", default="")


def current() -> Trace | None:
    """正在处理的这条消息的记录；不在处理消息时是 None。"""
    return _trace.get()


@contextlib.contextmanager
def purpose(name: str) -> Iterator[None]:
    """这里面发起的模型调用，不属于哪条消息的话，记在 ``name`` 这个环节名下。"""
    token = _purpose.set(name)
    try:
        yield
    finally:
        _purpose.reset(token)


def current_step() -> Step | None:
    return _step.get()


def forget_current() -> None:
    """正在处理的这条消息是用户在要求删除他的数据：这条消息自己的记录也不要留。

    不然刚删完，「帮我把我的数据删了」这句话和它引出的模型调用（提示词里带着他的记忆和上文）
    又存了进去。已经存下的那几次模型调用在处理完时一并删掉（见 :meth:`Tracer.finish`）。
    """
    trace = _trace.get()
    if trace is not None:
        trace.forgotten = True


@contextlib.contextmanager
def span(kind: str, title: str = "", **data: Any) -> Iterator[Step]:
    """把一个阶段包起来：记下它什么时候开始、用了多久、成没成；阶段的结果往拿到的 ``Step`` 里填。

    这个阶段里发生的模型调用会记在它名下。没有在记录时照常执行，填进去的东西不留。
    """
    trace = _trace.get()
    if trace is None:
        yield Step(id=0, kind=kind, title=title, at=0.0, data=dict(data))
        return
    step = trace.open(kind, title, dict(data), timed=True)
    token = _step.set(step)
    started = time.perf_counter()
    try:
        yield step
    except BaseException as exc:
        step.ok = False
        step.data.setdefault("error", brief(f"{type(exc).__name__}: {exc}"))
        raise
    finally:
        step.seconds = time.perf_counter() - started
        with contextlib.suppress(ValueError):  # 进出不在同一个上下文里时还原不了，不要紧
            _step.reset(token)


def event(kind: str, title: str = "", **data: Any) -> None:
    """记一件一闪而过的事（发了一句进度、走了哪条路）。"""
    trace = _trace.get()
    if trace is not None:
        trace.open(kind, title, dict(data), timed=False)


def note(
    *,
    route: str | None = None,
    command: str | None = None,
    outcome: str | None = None,
    **summary: Any,
) -> None:
    """在整条记录上记结论：走了哪条路、查证的结果；其余的进 ``summary``。"""
    trace = _trace.get()
    if trace is None:
        return
    if route is not None:
        trace.route = route
    if command is not None:
        trace.command = command
    if outcome is not None:
        trace.outcome = outcome
    trace.summary.update(summary)


class TraceSink(Protocol):
    async def save_trace(self, trace: Trace) -> None: ...

    async def save_call(self, call: LLMCall) -> None: ...

    async def erase_trace(self, trace_id: str) -> None:
        """删掉一条记录和它名下已经存了的模型调用。"""
        ...


class Tracer:
    """管处理记录的开始、结束和落地。"""

    def __init__(
        self, sink: TraceSink | None = None, *, keep_prompts: bool = True, recent: int = 50
    ):
        self.sink = sink
        #: 记不记每次模型调用的完整提示词和回复
        self.keep_prompts = keep_prompts
        #: 正在处理的
        self.active: dict[str, Trace] = {}
        #: 刚处理完的最近几条（没有数据库时，管理后台也能看到最近的）
        self.recent: deque[Trace] = deque(maxlen=recent)
        self._pending: set[asyncio.Task[None]] = set()

    def begin(self, message: InboundMessage) -> Trace:
        trace = Trace(message)
        trace._tokens = (_trace.set(trace), _step.set(None))
        self.active[trace.id] = trace
        # 处理这条消息期间打的每一行日志都带上记录的编号：日志里不写用户说了什么，
        # 要看是哪个问题，拿编号去管理后台的处理记录里查（那边按保留期清理、用户要求时能删）
        bind_contextvars(trace=trace.id)
        return trace

    def finish(
        self,
        trace: Trace,
        *,
        reply: OutboundMessage | None = None,
        error: BaseException | None = None,
    ) -> None:
        trace.seconds = trace.elapsed
        if error is not None:
            trace.status = ERROR
            trace.error = brief(f"{type(error).__name__}: {error}", 1000)
        elif reply is not None:
            trace.status = REPLIED
            trace.reply = reply.content
        else:
            trace.status = SILENT
        if not trace.route:
            trace.route = "none"
        if trace._tokens is not None:
            with contextlib.suppress(ValueError):
                _step.reset(trace._tokens[1])
                _trace.reset(trace._tokens[0])
            trace._tokens = None
        unbind_contextvars("trace")
        self.active.pop(trace.id, None)
        if trace.forgotten:
            # 用户要求删除数据的那条消息：记录不存；在他提出之前已经存下的模型调用删掉。
            # 那几次的存盘可能还在后台排着，等它们落完再删，不然先删后存、又留下了
            if self.sink is not None:
                waiting = list(self._pending)
                self._background(self._erase(trace.id, waiting), "trace_erase_failed")
            return
        self.recent.append(trace)
        if self.sink is not None:
            self._background(self.sink.save_trace(trace), "trace_save_failed")

    async def _erase(self, trace_id: str, waiting: list[asyncio.Task[None]]) -> None:
        if waiting:
            await asyncio.gather(*waiting, return_exceptions=True)
        if self.sink is not None:
            await self.sink.erase_trace(trace_id)

    def record_call(self, call: LLMCall) -> None:
        """模型调用层每调一次模型交来一份。它属于哪条消息、哪一步，从当时的上下文里认。"""
        trace, step = _trace.get(), _step.get()
        if trace is not None:
            call.trace_id = trace.id
            trace.calls.append(call)
        if step is not None:
            call.step_id = step.id
            call.purpose = call.purpose or step.kind
        call.purpose = call.purpose or _purpose.get()
        if not self.keep_prompts:
            call.request = call.response = None
        if trace is not None and trace.forgotten:
            return  # 用户刚要求删除他的数据，这条消息后面的调用不存
        if self.sink is not None:
            self._background(self.sink.save_call(call), "llm_call_save_failed")

    def _background(self, work: Any, failure: str) -> None:
        async def run() -> None:
            try:
                await work
            except Exception as exc:  # 记录存不进去不能影响回复
                logger.warning(failure, error=brief(repr(exc)))

        try:
            task = asyncio.get_running_loop().create_task(run())
        except RuntimeError:  # 没有事件循环（同步的测试里）：不存
            work.close()
            return
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def drain(self) -> None:
        """等后台没存完的都存完（进程退出前调用）。"""
        while self._pending:
            await asyncio.gather(*list(self._pending), return_exceptions=True)
