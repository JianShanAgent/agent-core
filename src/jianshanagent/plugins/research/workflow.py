"""查证工作流：弄明白问题 → 逐级取证 → 查证员写结论 → 复核员把关 → 不通过就打回。

顺序、升级和打回都由这里的代码定死，模型只做几件事：分析问题、判断资料够不够（并写结论）、复核结论。

0. 先弄明白问题（analysis.py）：归类、摘出里面的名字去 wiki 数据里查别名、把「今年」这类相对时间
   换成具体的。查到的对照关系用在三处：检索时补进问句；点到名的条目，从它自己的页面里挑几片
   排在资料最前面；作答和复核时告诉模型。
   用户发了图片的话，先以图搜图，找到的相似图片也算资料。
1. 取证分级，按配置的顺序排好。一级里可以有几个来源，一次查完才交给查证员
   （比如知识库和站内搜索一起查：站内搜索的词用第 0 步挑的，不用等查证员看过资料再给）。先查第一级。
2. 查证员看问题和目前的全部资料：够就写结论；不够就给出搜索词，
   升级到下一级，资料累加后再判断。最后一级也查过了，就只能用现有资料回答，答不了的直说没查到。
3. 结论交给复核员（另一次带思考的调用）：对着问题和全部资料逐句核对。
   通过就结束；不通过就带着理由打回给查证员，由它改写，或者接着升级取证。
4. 被打回、但已经没法再走一轮（打回次数超过 ``max_rejections``，或者这时已经花了超过
   ``max_seconds``）：不再让查证员重写，改由一次不开思考的调用按复核意见只改被点名的地方，
   改完直接发（实测有过等了三分半钟、只因为一处小错就回「没查到」的情况）。
   没配这一步，或者改不出来，才如实说没查到确切信息。

某一级取证出错或超时，当成这一级什么都没查到，接着往下走。问题分析、复核本身做不成（接口出错）时
不拦流程。查证员、复核员那两次带思考的调用没成时（思考把额度用光、超时），用不开思考的那一档再做一次。
查证员配的就是不开思考的那一档时没有这第二次机会，所以它超时了原样再问一次（``quick_retries``）；
被打回后重写的那一次除外——那时手里已经有一版结论，重写没做成就照第 4 步按复核意见修正它。

时间上有三道限制（模型接口忙的时候个别请求会卡一两分钟，不能让用户跟着等）：
带思考的调用最多等 ``think_timeout_seconds``，不开思考的最多等 ``quick_timeout_seconds``，
整个查证不超过 ``deadline_seconds``（群聊里平台只给 5 分钟回复，超过了结论就发不出去了）。

查证要花几十秒，所以每到一个阶段报告一次，由调用方发一句进度给用户。阶段用代号表示：
analyze、image、取证来源的 ``name``（kb / site / web）、review、revise、fix。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import tzinfo
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel, Field

from jianshanagent.core import trace
from jianshanagent.core.clock import now_text
from jianshanagent.core.llm import LLMClient, LLMContentBlocked, LLMError, user_message
from jianshanagent.core.logging import get_logger
from jianshanagent.plugins.research.analysis import (
    DEFAULT_TOPICS,
    NameLookup,
    Understanding,
    Wording,
    understand,
)
from jianshanagent.plugins.research.evidence import (
    EXCERPT,
    NO_SOURCES,
    Evidence,
    EvidenceSource,
    Sources,
    image_evidence,
    render,
)

logger = get_logger(__name__)

ANSWERED = "answered"
NOT_FOUND = "not_found"
FAILED = "failed"

ANALYZE = "analyze"
IMAGE = "image"
REVIEW = "review"
REVISE = "revise"
FIX = "fix"
#: 不算「取证级别」的阶段（统计查到第几级时用）
OTHER_STAGES = frozenset({ANALYZE, IMAGE, REVIEW, REVISE, FIX})

#: 剩下的时间不到这么多秒，就不值得再开一次带思考的调用了
_MIN_THINK_SECONDS = 10.0

#: 每到一个阶段调用一次，参数是要发给用户的那句进度
OnProgress = Callable[[str], Awaitable[None]]
ModelT = TypeVar("ModelT", bound=BaseModel)


class EntrySource(Protocol):
    """点到名的条目自己的内容（见 evidence.py 的 ``EntryPages``）。"""

    async def collect(
        self, question: str, titles: Sequence[str], have: Collection[str]
    ) -> list[Evidence]: ...


class PageSource(Protocol):
    """把挑中的页面补齐（见 evidence.py 的 ``PageFiller``）。"""

    async def fill(self, evidence: Sequence[Evidence], start: int, room: int) -> list[Evidence]: ...


class ImageSearcher(Protocol):
    """以图搜图（knowledge 插件的 ``image_search`` 服务）。"""

    async def search(self, image: bytes, *, top_k: int | None = None) -> Sequence[Any]:
        """返回和这张图相似的 wiki 图片（带 label / kind / url / score / sure / text）。"""


@dataclass(frozen=True, slots=True)
class WorkflowSettings:
    #: 分析问题用哪一档模型；空 = 不分析，按原话查
    analysis_profile: str = ""
    analyst_profile: str = "reason"
    reviewer_profile: str = "review"
    #: 不开思考、要快的那一档，两个用处：带思考的调用没成（思考把额度用光、超时、输出不合格式）时
    #: 用它再做一次；没法再走一轮时用它按复核意见修正。空 = 这两件事都不做
    quick_profile: str = ""
    #: 不开思考的调用最多等多久（平时几秒；接口排队时会卡很久，不能跟着干等）
    quick_timeout_seconds: float = 30.0
    #: 查证员配的就是不开思考的那一档时，它那次调用超时了再问几次（没有别的档位可换）。
    #: 这一步做不成整个查证就失败，而超时常常只是那一个请求卡住了
    quick_retries: int = 1
    #: 带思考的调用最多等多久（平时 5～40 秒）；超过了就不等了，改用不开思考的那一档
    think_timeout_seconds: float = 75.0
    #: 哪些档位是不开思考的。查证员 / 复核员配成了这些档位时，那次调用按 ``quick_timeout_seconds``
    #: 限时（没必要按带思考的等那么久），没成照样换 ``quick_profile`` 再做一次
    fast_profiles: frozenset[str] = frozenset()
    #: 整个查证的最后期限：各次调用的限时都不会越过它。到点还没有结论就按当时的情况收尾
    #: （有通过了的结论就发，没有就如实说没查到）
    deadline_seconds: float = 240.0
    analysis_timeout_seconds: float = 8.0
    analysis_topics: tuple[str, ...] = DEFAULT_TOPICS
    #: 提示词里因站而异的说法
    wording: Wording = field(default_factory=Wording)
    #: 一个问题里最多查几个名字的别名
    max_names: int = 5
    #: 一条消息最多拿几张图去以图搜图
    max_images: int = 2
    #: 以图搜图查到的资料上标的来源名
    image_label: str = "以图搜图"
    #: 复核最多打回几次
    max_rejections: int = 2
    max_citations: int = 5
    #: 给模型看的资料总字数上限；超了的不要（先查到的优先）
    evidence_chars: int = 24000
    #: 回答一般不超过多少字（写进给查证员的要求里；越长越慢，也越容易出错）
    answer_chars: int = 300
    #: 被打回时整个查证已经花了超过这么久，就不再让查证员重写一轮
    max_seconds: float = 100.0
    #: 每个阶段发给用户的那句进度（阶段代号 → 文字）；没写的阶段不发
    progress: Mapping[str, str] = field(default_factory=dict)


class Decision(BaseModel):
    """查证员的输出。"""

    #: answer = 资料够了，answer 里是结论；search = 不够，keywords 里是下一级的搜索词
    action: str = "answer"
    answer: str = ""
    sources: list[int] = Field(default_factory=list)
    keywords: str = ""


class Verdict(BaseModel):
    """复核员的输出。"""

    #: 过没过必须明说：没写就当通过的话，只列了问题、漏写这一项的复核会被当成通过
    ok: bool
    problems: list[str] = Field(default_factory=list)
    sources: list[int] = Field(default_factory=list)


class Fixed(BaseModel):
    """按复核意见修正后的回答。"""

    answer: str = ""


@dataclass(slots=True)
class Outcome:
    #: answered = 有结论；not_found = 没查到确切信息；failed = 模型接口出错
    status: str
    answer: str = ""
    #: 结论引用到的资料
    cited: list[Evidence] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    #: 走过的阶段（代号）
    steps: list[str] = field(default_factory=list)
    rejections: int = 0
    #: 最后一次复核指出的问题（通过了就是空的）
    problems: list[str] = field(default_factory=list)
    #: 结论是按复核意见快速修正出来的（修正后没有再复核）
    fixed: bool = False
    #: 检索实际用的问句（补了正式名和具体时间）
    query: str = ""
    #: 取证查到了第几级（一级里可以有几个来源）
    levels: int = 0
    seconds: float = 0.0


_ANALYST = """你是{bot}的查证员。下面是用户的问题和目前查到的全部资料。
{task}

现在是 {now}。

写回答的规则：
1. 只用资料里有原文依据的内容：每一个{facts}，
   都要能在资料里找到原话。不要凭记忆补充，不要猜。
2. 不要把几条资料拼出资料里没有的结论，不要把{mixups}的内容安到问的对象上。{analyst_note}
3. 问题里资料答不了的部分直说没查到，不要含糊带过，也不要编。
   标着「{excerpt}」的资料只是所在页面的一部分，页面上还有没给出来的内容：
   要列全的名单、要数的总数，如果只出现在{excerpt}里，照着列出来的同时说明可能不全。
4. 资料开头标着它是从哪里查到的。标着「{web}」的是网上搜来的，不是 wiki 的内容：
   只在别的资料答不了时才用，用到时说明是网上搜到的，和别的资料矛盾时以别的资料为准。
   其余的都是 wiki 收录的内容（包括 wiki 收录的官方公告），不用特别说明出处。
5. 只回答用户问的那件事：先说结论，再给必要的依据，用户没问的不要展开。
   概括性的问题（「X 是什么」「X 怎么样」）用两三句话说清是什么、最要紧的一两点，再给页面链接，
   不要把资料里的细节都搬出来——写得越多越容易把不同时期的内容混在一起。
   除非用户明确要清单或完整的数据，回答不超过 {answer_chars} 个字。
6. 不要提「资料」「编号」「查证」这类字眼，不要复述问题。要给链接只用资料里的链接。
7. sources 写回答实际依据的资料编号，最多 {max_citations} 个，按重要程度排。
{style}{feedback}
用户的问题：
{question}
{notes}
目前查到的资料：
{evidence}
"""

# 还有下一级可查：查证员可以选择升级
_TASK_MORE = """先判断资料够不够回答这个问题，再按情况输出，只返回 JSON：
{{"action": "answer" 或 "search", "answer": "给用户的完整回答", "sources": [资料编号, …],
  "keywords": "搜索词"}}

怎么判断：
- 资料里有问题问的内容：action 为 "answer"，写出回答，keywords 留空。
- 资料里没有，或者关键的地方对不上（名字、年份、期数不是问的那个）、只答得了一小部分：
  action 为 "search"，answer 留空，在 keywords 里写下一步「{label}」要用的搜索词。{hint}"""

# 最后一级也查过了：只能用现有的资料回答
_TASK_LAST = """已经没有别的地方可查了。用现有的资料回答这个问题，只返回 JSON：
{{"action": "answer", "answer": "给用户的完整回答", "sources": [资料编号, …]}}

资料只答得了一部分，就答那一部分，其余的直说没查到；完全答不了，就在 answer 里直说没查到
（可以说明查到了哪些相近的内容）。answer 不能留空。"""

_FEEDBACK = """
你上一次写的回答被复核员打回了，这次必须改正（资料确实不够就继续搜索，不要硬答）。
上一次的回答：
{draft}
复核员指出的问题：
{problems}
"""

_NOTES = """
程序预先查到的（出自 wiki 的数据和图库，可以当依据）：
{lines}
"""

_REVIEWER = """你是严格的事实复核员。下面是用户的问题、查到的全部资料、以及查证员写好的回答。
逐句核对回答，只返回 JSON：
{{"ok": true 或 false, "problems": ["问题一", …], "sources": [资料编号, …]}}

现在是 {now}。

核对规则：
1. 回答里的每一个事实（{facts}）
   都必须能在资料里找到原文依据。资料里没有的，哪怕你觉得是对的，也算没有依据。
2. 把几条资料拼在一起得出资料里没有的结论，
   或者把{mixups}的内容安到问的对象上，都算错。{reviewer_note}
3. 资料里有答案，回答却说没查到，或者漏掉了问题问的关键内容，也算有问题。
4. 答非所问（回答的不是用户问的那件事）也算有问题。
5. 只挑事实上的毛病。语气、详略、排版不算；资料里确实没有、回答如实说了没查到，不算问题。

输出规则：
- 没有问题：ok 为 true，problems 为空列表。
- 有问题：ok 为 false，problems 里逐条写清楚：回答里的哪句话、错在哪、资料里是怎么写的
  （带上资料编号），让查证员能照着改。
- sources：回答里的事实实际依据的资料编号，最多 {max_citations} 个，按重要程度排；没有就给空列表。

用户的问题：
{question}
{notes}
查到的资料：
{evidence}

查证员的回答：
{answer}
"""

_FIXER = """你是{bot}的编辑。下面是一份写给用户的回答、复核员对它提出的问题、
以及查到的资料。按复核员的意见把回答改好，只返回 JSON：
{{"answer": "改好的完整回答"}}

规则：
1. 只动复核员点名的地方：没有依据的内容删掉；和资料矛盾的按资料改正（意见里写了该怎么改就照着改）。
2. 其余的句子、排版、语气、链接一律原样保留；不要新增任何内容；不要提「复核」「资料」这类字眼。
3. 改完以后如果已经没有能回答问题的内容了，answer 给空字符串。

用户的问题：
{question}
{notes}
查到的资料：
{evidence}

原来的回答：
{draft}

复核员指出的问题：
{problems}
"""


class ResearchWorkflow:
    def __init__(
        self,
        llm: LLMClient,
        sources: Sequence[EvidenceSource | Sequence[EvidenceSource]],
        settings: WorkflowSettings | None = None,
        *,
        timezone: tzinfo | None = None,
        name_lookup: NameLookup | None = None,
        entry_pages: EntrySource | None = None,
        image_search: ImageSearcher | None = None,
        page_filler: PageSource | None = None,
        wiki_labels: Sources = NO_SOURCES,
    ):
        """``sources`` 是取证的各级，按先后排；一级可以是一个来源，也可以是一起查的几个来源。
        ``wiki_labels``：挂了不止一个 wiki 时各个 wiki 的叫法（以图搜图的资料上标出处用）。"""
        #: 取证的各级。一级里的几个来源一次查完（按顺序，后面的不重复取前面已经取到的），
        #: 查完才交给查证员；同一个来源可以出现在不止一级里
        self.levels: list[list[EvidenceSource]] = [
            list(level) if isinstance(level, Sequence) else [level] for level in sources
        ]
        if not self.levels or not all(self.levels):
            raise ValueError("查证工作流至少要有一级取证来源，每一级至少一个来源")
        self.llm = llm
        #: 用到的全部来源（不重复，按出现的先后）
        self.sources: list[EvidenceSource] = list(
            {id(source): source for level in self.levels for source in level}.values()
        )
        self.settings = settings or WorkflowSettings()
        self.timezone = timezone
        self.name_lookup = name_lookup
        self.entry_pages = entry_pages
        self.image_search = image_search
        self.page_filler = page_filler
        self.wiki_labels = wiki_labels

    @staticmethod
    def _notes(lines: Sequence[str]) -> str:
        return _NOTES.format(lines="\n".join(f"- {line}" for line in lines)) if lines else ""

    async def _ask(
        self, prompt: str, schema: type[ModelT], profile: str, until: float, *, retries: int = 0
    ) -> ModelT:
        """用 ``profile`` 这一档问一次；没成，就用不开思考的那一档再问一次（没配就把错误抛出去）。

        带思考的调用不在原地重试：它失败多半是思考把额度用光了，或者接口在排队，重来一遍还是一两分钟。
        ``until`` 是整个查证的最后期限（``time.perf_counter`` 的读数）：思考最多等到
        「给不开思考的那一次留够时间」为止，剩下的时间不够想了就直接用不开思考的。
        ``profile`` 本身不开思考时（``fast_profiles``）只等 ``quick_timeout_seconds``。
        ``retries``：``profile`` 就是兜底的那一档时（没有别的档位可换），超时了再问几次。
        """
        settings = self.settings
        if profile == settings.quick_profile:  # 配的就是兜底的那一档：不用先试一次再兜底
            return await self._quick(prompt, schema, until, retries=retries)
        reserve = settings.quick_timeout_seconds if settings.quick_profile else 0.0
        budget = until - time.perf_counter() - reserve  # 留给这一次的时间
        if budget >= _MIN_THINK_SECONDS or not settings.quick_profile:
            fast = profile in settings.fast_profiles
            limit = settings.quick_timeout_seconds if fast else settings.think_timeout_seconds
            think = max(min(limit, budget), 0.001)
            try:
                return await asyncio.wait_for(
                    self.llm.chat_json([user_message(prompt)], schema, profile=profile, attempts=1),
                    think,
                )
            except LLMContentBlocked:
                raise
            except (LLMError, TimeoutError) as exc:
                if not settings.quick_profile:
                    raise LLMError(f"带思考的调用没成：{exc!r}") from None
                logger.warning(
                    "research_thinking_call_failed",
                    profile=profile,
                    waited=round(think, 1),
                    error=repr(exc)[:300],
                )
        return await self._quick(prompt, schema, until)

    async def _quick(
        self, prompt: str, schema: type[ModelT], until: float, *, retries: int = 0
    ) -> ModelT:
        """用不开思考的那一档问，限时（不越过最后期限）；超时了最多再问 ``retries`` 次。

        只有超时才再问：接口报错、输出不合格式在 ``chat_json`` 里面已经重试过了。
        """
        settings = self.settings
        for attempt in range(retries + 1):
            timeout = min(settings.quick_timeout_seconds, until - time.perf_counter())
            if timeout <= 0:
                raise LLMError("已经到了查证的最后期限")
            try:
                return await asyncio.wait_for(
                    self.llm.chat_json(
                        [user_message(prompt)], schema, profile=settings.quick_profile
                    ),
                    timeout,
                )
            except TimeoutError:
                if attempt == retries:
                    raise LLMError(f"{timeout:.0f} 秒没有回应") from None
                logger.warning(
                    "research_quick_call_timed_out", waited=round(timeout, 1), retrying=True
                )
        raise AssertionError("循环里要么返回要么抛出")

    async def _analyze(
        self,
        question: str,
        notes: Sequence[str],
        evidence: list[Evidence],
        next_level: Sequence[EvidenceSource] | None,
        searched: Mapping[str, Sequence[str]],
        draft: str,
        problems: list[str],
        style: str,
        until: float,
    ) -> Decision | None:
        """``next_level`` 是还能再查的下一级（没有了就是 None）；
        ``searched`` 是各个来源已经用过的搜索词。"""
        if next_level is None:
            task = _TASK_LAST.format()
        else:
            hints = [source.keywords_hint for source in next_level if source.keywords_hint]
            for source in next_level:
                used = "」「".join(dict.fromkeys(searched.get(source.name, ())))
                if used:  # 这个来源前面已经查过（比如和知识库一起查的那次）：别拿同样的词再搜一遍
                    hints.append(f"「{source.label}」已经用「{used}」搜过，这次要换不一样的词。")
            task = _TASK_MORE.format(
                label="、".join(source.label for source in next_level), hint="".join(hints)
            )
        feedback = ""
        if problems:
            feedback = _FEEDBACK.format(
                draft=draft, problems="\n".join(f"- {problem}" for problem in problems)
            )
        wording = self.settings.wording
        prompt = _ANALYST.format(
            bot=wording.bot,
            facts=wording.facts,
            mixups=wording.mixups,
            analyst_note=f"\n   {wording.analyst_note}" if wording.analyst_note else "",
            task=task,
            now=now_text(self.timezone),
            web=next((source.label for source in self.sources if source.name == "web"), "联网搜索"),
            excerpt=EXCERPT,
            max_citations=self.settings.max_citations,
            answer_chars=self.settings.answer_chars,
            style=f"8. {style}\n" if style else "",
            feedback=feedback,
            question=question,
            notes=self._notes(notes),
            evidence=render(evidence),
        )
        # 已经有一版结论（这次是被打回后重写）：重写没做成还能按复核意见修正它，不用再问一次
        retries = 0 if draft else self.settings.quick_retries
        try:
            return await self._ask(
                prompt, Decision, self.settings.analyst_profile, until, retries=retries
            )
        except LLMContentBlocked:
            raise
        except LLMError as exc:
            logger.warning("research_analyst_failed", error=str(exc)[:300])
            return None

    async def _review(
        self,
        question: str,
        notes: Sequence[str],
        evidence: list[Evidence],
        draft: str,
        until: float,
    ) -> Verdict | None:
        wording = self.settings.wording
        prompt = _REVIEWER.format(
            facts=wording.facts,
            mixups=wording.mixups,
            reviewer_note=f"\n   {wording.reviewer_note}" if wording.reviewer_note else "",
            now=now_text(self.timezone),
            max_citations=self.settings.max_citations,
            question=question,
            notes=self._notes(notes),
            evidence=render(evidence),
            answer=draft,
        )
        try:
            return await self._ask(prompt, Verdict, self.settings.reviewer_profile, until)
        except LLMContentBlocked:
            # 同样的问题和资料查证员那次没被拦，这次多出来的只有那份回答：被拦的多半就是它，不能照发
            raise
        except LLMError as exc:  # 复核做不成不拦结论
            logger.warning("research_review_failed", error=str(exc)[:300])
            return None

    async def _fix(
        self,
        question: str,
        notes: Sequence[str],
        evidence: list[Evidence],
        draft: str,
        problems: Sequence[str],
        until: float,
    ) -> str:
        """按复核意见只改被点名的地方；改不出来返回空字符串。"""
        prompt = _FIXER.format(
            bot=self.settings.wording.bot,
            question=question,
            notes=self._notes(notes),
            evidence=render(evidence),
            draft=draft,
            problems="\n".join(f"- {problem}" for problem in problems),
        )
        try:
            fixed = await self._quick(prompt, Fixed, until)
        except LLMError as exc:
            logger.warning("research_fix_failed", error=str(exc)[:300])
            return ""
        return fixed.answer.strip()

    def _cited(self, numbers: Sequence[int], evidence: list[Evidence]) -> list[Evidence]:
        """按编号取出引用的资料，同一条只留一次。网上搜来的网页没有能发出去的链接，列出来时只有标题。"""
        picked: dict[str, Evidence] = {}
        for number in numbers:
            if 1 <= number <= len(evidence):
                item = evidence[number - 1]
                if item.url or (item.key and item.title):
                    picked.setdefault(item.url or item.key, item)
        return list(picked.values())[: self.settings.max_citations]

    async def _look_at_images(
        self, images: Sequence[bytes]
    ) -> tuple[list[Evidence], str, list[Any]]:
        """以图搜图。返回（相似图片整理成的资料，给模型的一句说明，认准了的那几张 wiki 图片）。"""
        settings = self.settings
        images = list(images)[: settings.max_images]
        found: list[Evidence] = []
        identified: list[Any] = []
        if self.image_search is not None:
            for number, image in enumerate(images, start=1):
                try:
                    hits = list(await self.image_search.search(image))
                except Exception as exc:  # 图片认不出、嵌入接口出错：当成没搜到
                    logger.warning("research_image_search_failed", error=repr(exc)[:300])
                    hits = []
                found += image_evidence(
                    hits,
                    origin=settings.image_label,
                    number=number,
                    total=len(images),
                    sources=self.wiki_labels,
                )
                identified += [hit for hit in hits[:1] if hit.sure]
                logger.info(
                    "research_image_searched",
                    image=number,
                    hits=[(str(hit.label), round(float(hit.score), 3)) for hit in hits],
                )
        if found:
            note = (
                f"用户发了 {len(images)} 张图片，在 wiki 图库里找到的相似图片列在资料里"
                f"（标着「{settings.image_label}」的）：写着「认定就是这张图」的，直接当成图片的身份；"
                "只是「有些像」的，回答时要说明不能确定。"
            )
        else:
            note = (
                f"用户发了 {len(images)} 张图片，但在 wiki 图库里没有找到对得上的图。"
                "回答时如实说没认出图片，不要猜图里是谁、是什么。"
            )
        return found, note, identified

    async def run(
        self,
        question: str,
        *,
        style: str = "",
        on_progress: OnProgress | None = None,
        images: Sequence[bytes] = (),
    ) -> Outcome:
        """查证一个问题。

        ``question`` 要是一句不依赖上文的完整问题；``style`` 是对回答口吻、排版的要求；
        ``images`` 是用户随问题发来的图片（原始字节）。
        内容被平台审核拦截时抛 :class:`LLMContentBlocked`，其余情况都返回 :class:`Outcome`。
        """
        started = time.perf_counter()
        settings = self.settings
        until = started + settings.deadline_seconds
        question = question.strip()
        outcome = Outcome(status=FAILED, query=question)
        evidence, steps = outcome.evidence, outcome.steps
        used = 0  # 已有资料的总字数
        level = -1  # 查到第几级了
        searched: dict[str, list[str]] = {}  # 各个来源用过的搜索词

        async def step(stage: str) -> None:
            steps.append(stage)
            text = settings.progress.get(stage)
            if text and on_progress is not None:
                try:
                    await on_progress(text)
                except Exception:  # 进度发不出去不影响查证
                    logger.exception("research_progress_failed")

        def add(found: Sequence[Evidence]) -> int:
            """把新查到的资料并进来（重复的、放不下的不要），返回新增了几条。"""
            nonlocal used
            have = {item.key for item in evidence}
            added = 0
            for item in found:
                if item.key in have or (
                    evidence and used + len(item.text) > settings.evidence_chars
                ):
                    continue
                have.add(item.key)
                evidence.append(item)
                used += len(item.text)
                added += 1
            return added

        def listed(start: int) -> list[dict[str, Any]]:
            """处理记录里的资料清单：第 ``start`` 条往后新增的。编号和给模型看的【资料 N】一致。"""
            return [
                {
                    "number": number,
                    "origin": item.origin,
                    "source": item.source,
                    "kind": item.kind,
                    "title": item.title,
                    "url": item.url,
                    "date": item.date,
                    "key": item.key,
                    "chars": len(item.text),
                    "excerpt": item.excerpt,
                    "preview": trace.brief(item.text, 200),
                }
                for number, item in enumerate(evidence[start:], start=start + 1)
            ]

        async def collect(keywords: str) -> int:
            """查下一级（里面的来源挨个查，后面的不重复取前面取到的），返回新增了几条资料。"""
            nonlocal level, used
            level += 1
            start = len(evidence)  # 这一级的资料从这里开始
            group = self.levels[level]
            await step(group[0].name)  # 一级只报一句进度，用第一个来源的
            steps.extend(source.name for source in group[1:])
            added = 0
            for position, source in enumerate(group):
                have = len(evidence)
                with trace.span(
                    source.name,
                    f"查{source.label}",
                    keywords=keywords if source.keywords_hint else "",
                ) as record:
                    if position == 0 and level == 0 and entries and self.entry_pages is not None:
                        # 点到名的条目自己的内容排在最前面
                        own = 0
                        try:
                            own = add(
                                await self.entry_pages.collect(
                                    outcome.query, entries, {e.key for e in evidence}
                                )
                            )
                        except Exception as exc:
                            logger.warning("research_entry_pages_failed", error=repr(exc)[:300])
                        added += own
                        record.set(entries=entries, from_entries=own)
                    try:
                        found = await source.collect(
                            outcome.query, keywords, {e.key for e in evidence}
                        )
                    except Exception as exc:
                        logger.warning(
                            "research_source_failed", source=source.name, error=repr(exc)[:300]
                        )
                        record.set(error=trace.brief(repr(exc)))
                        found = []
                    new = add(found)
                    added += new
                    record.set(query=outcome.query, found=len(found), evidence=listed(have))
                if keywords and source.keywords_hint:  # 知识库这类不看搜索词的来源不用记
                    searched.setdefault(source.name, []).append(keywords)
                logger.info(
                    "research_collected",
                    source=source.name,
                    found=len(found),
                    added=new,
                    keyword_chars=len(keywords),
                )
            if self.page_filler is not None and len(evidence) > start:
                # 同一个页面挑中了好几片的，把这一页剩下的也补上
                with trace.span("fill", "把挑中的页面补全") as record:
                    try:
                        filled = await self.page_filler.fill(
                            evidence, start, settings.evidence_chars - used
                        )
                    except Exception as exc:
                        logger.warning("research_page_fill_failed", error=repr(exc)[:300])
                        record.ok = False
                        record.set(error=trace.brief(repr(exc)))
                    else:
                        grown = len(filled) - len(evidence)
                        evidence[:] = filled
                        total = sum(len(item.text) for item in evidence)
                        logger.info(
                            "research_pages_filled",
                            added=grown,
                            chars=total - used,
                            excerpts=sum(item.excerpt for item in evidence),
                        )
                        record.set(
                            added=grown,
                            chars=total - used,
                            excerpts=sum(item.excerpt for item in evidence),
                        )
                        used = total
                        added += grown
            return added

        def done(status: str) -> Outcome:
            outcome.status = status
            outcome.levels = level + 1
            outcome.seconds = time.perf_counter() - started
            logger.info(
                "research_done",
                status=status,
                steps=steps,
                evidence=len(evidence),
                rejections=outcome.rejections,
                fixed=outcome.fixed,
                seconds=round(outcome.seconds, 1),
                question_chars=len(question),
            )
            trace.note(
                outcome=status,
                research={
                    "topic": understanding.topic,
                    "query": outcome.query,
                    "stages": list(steps),
                    "evidence": len(evidence),
                    "rejections": outcome.rejections,
                    "fixed": outcome.fixed,
                    "problems": outcome.problems,
                    "cited": [{"title": item.title, "url": item.url} for item in outcome.cited],
                    "seconds": round(outcome.seconds, 2),
                },
            )
            return outcome

        async def fix_or(otherwise: str, sources: Sequence[int]) -> Outcome:
            """第 4 步：按复核意见只改被点名的地方就发。

            没配这一步或者改不出来，按 ``otherwise`` 收尾。``sources`` 是那一版结论引用的资料编号。
            """
            if settings.quick_profile:
                await step(FIX)
                with trace.span(FIX, "按复核意见快速修正", problems=outcome.problems) as record:
                    fixed = await self._fix(
                        question, notes, evidence, draft, outcome.problems, until
                    )
                    record.set(answer=fixed)
                if fixed:
                    outcome.answer = fixed
                    outcome.cited = self._cited(sources, evidence)
                    outcome.fixed = True
                    return done(ANSWERED)
            return done(otherwise)

        trace.note(route="research")
        # 0. 弄明白问题；有图片先以图搜图
        understanding = Understanding()
        if settings.analysis_profile:
            await step(ANALYZE)
            with trace.span(ANALYZE, "分析问题", question=question) as record:
                understanding = await understand(
                    self.llm,
                    question,
                    profile=settings.analysis_profile,
                    lookup=self.name_lookup,
                    topics=settings.analysis_topics,
                    timezone=self.timezone,
                    timeout_seconds=settings.analysis_timeout_seconds,
                    max_names=settings.max_names,
                    wording=settings.wording,
                )
                record.set(
                    topic=understanding.topic,
                    time=understanding.time,
                    names=[
                        {
                            "written": n.written,
                            "title": n.title,
                            "kind": n.kind,
                            "exact": n.exact,
                        }
                        for n in understanding.names
                    ],
                    query=understanding.search_query(question),
                )
        notes = understanding.notes()
        outcome.query = understanding.search_query(question)
        #: 问题里点到名、在 wiki 上确实有的条目（正式名）
        entries = list(dict.fromkeys(n.title for n in understanding.names if n.exact))
        if images:
            await step(IMAGE)
            with trace.span(IMAGE, settings.image_label, images=len(images)) as record:
                found, note, identified = await self._look_at_images(images)
                add(found)
                record.set(
                    identified=[str(hit.label) for hit in identified],
                    evidence=listed(0),
                )
            notes.append(note)
            if identified:
                # 认出来的图：它是什么补进检索问句，它所在的条目也先取自己页面里的内容
                labels = "；".join(dict.fromkeys(str(hit.label) for hit in identified))
                outcome.query = f"{outcome.query}（图片是：{labels}）"
                pages = [str(hit.page_title) for hit in identified if hit.page_title]
                entries = list(dict.fromkeys([*entries, *pages]))

        # 1–3. 逐级取证、写结论、复核
        # 第一级还没有查证员给的搜索词：用分析问题时挑的（知识库不看它；站内搜索排在第一级时用）
        await collect(understanding.keywords)
        draft = ""
        draft_sources: list[int] = []  # 被打回的那一版结论引用的资料编号
        while True:
            more = level + 1 < len(self.levels)
            title = "查证员按复核意见重写" if outcome.problems else "查证员看资料、写结论"
            # 资料清单记在这一步上：编号就是模型这一次看到的编号（补整页会让编号变）
            with trace.span("analyst", title, evidence=listed(0)) as record:
                decision = await self._analyze(
                    question,
                    notes,
                    evidence,
                    self.levels[level + 1] if more else None,
                    searched,
                    draft,
                    outcome.problems,
                    style,
                    until,
                )
                if decision is None:
                    record.ok = False
                    record.set(error="查证员这一步没做成（模型接口出错或超时）")
                else:
                    record.set(
                        action=decision.action.strip().lower(),
                        answer=decision.answer.strip(),
                        keywords=decision.keywords.strip(),
                        sources=decision.sources,
                    )
            if decision is None:
                if draft and outcome.problems:
                    # 被打回后重写没做成（接口出错、超时），但上一版结论还在：按复核意见修正它，
                    # 比让用户等了一分多钟只得到一句「有点忙」强
                    return await fix_or(FAILED, draft_sources)
                return done(FAILED)
            if decision.action.strip().lower() == "search" and more:
                keywords = decision.keywords.strip() or question
                # 这一级什么都没查到就直接再升一级，省一次模型调用
                while await collect(keywords) == 0 and level + 1 < len(self.levels):
                    pass
                continue
            draft = decision.answer.strip()
            if not draft:
                return done(NOT_FOUND)
            await step(REVIEW)
            with trace.span(REVIEW, "复核员核对结论", answer=draft) as record:
                verdict = await self._review(question, notes, evidence, draft, until)
                if verdict is None:
                    record.ok = False
                    record.set(error="复核没做成（模型接口出错或超时），结论照发")
                else:
                    record.set(
                        passed=verdict.ok, problems=verdict.problems, sources=verdict.sources
                    )
            if verdict is None or verdict.ok:
                numbers = verdict.sources if verdict and verdict.sources else decision.sources
                outcome.answer = draft
                outcome.cited = self._cited(numbers, evidence)
                outcome.problems = []
                return done(ANSWERED)
            outcome.rejections += 1
            outcome.problems = [str(p) for p in verdict.problems] or ["复核没有通过"]
            draft_sources = list(verdict.sources or decision.sources)
            logger.info(
                "research_rejected",
                times=outcome.rejections,
                problems=[p[:200] for p in outcome.problems[:5]],
            )
            out_of_time = time.perf_counter() - started > settings.max_seconds
            if out_of_time:
                logger.warning("research_out_of_time", seconds=settings.max_seconds)
            if not out_of_time and outcome.rejections <= settings.max_rejections:
                await step(REVISE)
                continue
            # 4. 没法再走一轮了：按复核意见只改被点名的地方
            return await fix_or(NOT_FOUND, draft_sources)
