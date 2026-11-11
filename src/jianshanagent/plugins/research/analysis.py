"""查证之前先把问题弄明白：问的是哪一类、里面有哪些名字、说的是什么时候。

用户说的常常是外号（「小小白」「高铁」）和相对时间（「今年」「上个月」），原样拿去检索会搜不到，
模型也不知道今年是哪一年。这里分两步：

1. 让一个便宜的模型（不开思考）把问题归类，摘出里面的名字，把相对时间换成具体的。
   名字只摘不改：外号对应谁不能让模型猜，由第 2 步去查。
2. 拿摘出来的名字去 wiki 数据里查别名（:data:`NameLookup`，实现见 plugin.py）：
   站点插件登记的昵称（阴阳师 wiki 的式神、御魂）、页面的重定向。

得到的 :class:`Understanding` 有两个用处：检索时在问句后面补上正式名和具体时间；
作答和复核时把这些对照关系告诉模型（它们来自 wiki 数据，可以当依据）。
模型顺带挑出站内全文搜索用的关键词（外号换成查到的正式名）：站内搜索排在第一级、
和知识库一起查的时候用它（那时查证员还没看过资料，给不出搜索词）。

哪一步出错都不拦查证：分析做不成，就按用户的原话查。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, tzinfo

from pydantic import BaseModel, Field

from jianshanagent.core.clock import now_text
from jianshanagent.core.llm import LLMClient, LLMError, user_message
from jianshanagent.core.logging import get_logger

logger = get_logger(__name__)

_MAX_KEYWORDS = 3  # 站内全文搜索的词最多几个（和要求查证员写搜索词时一样：1～3 个）

#: 问题归哪几类。因站而异，站点插件在配置里换成自己的（阴阳师：式神、御魂、技能机制……）
DEFAULT_TOPICS = ("人物", "事物", "事件", "规则机制", "其它")


@dataclass(frozen=True, slots=True)
class Wording:
    """提示词里因站而异的说法（``plugin.research`` 的 ``wording`` 段，站点插件换成自己的）。

    默认值是对什么 wiki 都说得通的说法。
    """

    #: 接在「你是」后面的自称（``WikiSubject.bot``）：「阴阳师 wiki 机器人」
    bot: str = " wiki 机器人"
    #: 向模型交代背景时怎么称呼这个 wiki（``WikiSubject.intro``）：「阴阳师（网易手游）wiki」
    intro: str = " wiki"
    #: 问题里要摘出来的名字都有哪些
    names: str = "条目、人物、事物、事件的名字或别称"
    #: 泛指的词，不算名字（「式神」「活动」）
    generic_words: tuple[str, ...] = ()
    #: 回答里必须有原文依据的事实都有哪些
    facts: str = "数值、日期、名字、引文"
    #: 容易张冠李戴的是什么
    mixups: str = "别的年份 / 别的对象"
    #: 这个站特有的提醒，接在查证员、复核员「不要张冠李戴」那一条后面；空 = 没有
    analyst_note: str = ""
    reviewer_note: str = ""
    #: 站内搜索的搜索词优先用哪些专有名词（「式神名、活动名」）、举个例子；空 = 不说
    keyword_nouns: str = ""
    keyword_example: str = ""


class Analysis(BaseModel):
    """模型对问题的分析。"""

    topic: str = ""
    #: 问题里出现的名字，照用户的写法
    names: list[str] = Field(default_factory=list)
    #: 问题里的相对时间换成的具体时间；没有就是空的
    time: str = ""
    #: 站内全文搜索该用的关键词，照用户的写法
    keywords: list[str] = Field(default_factory=list)


@dataclass(frozen=True, slots=True)
class NameNote:
    """一个名字在 wiki 数据里查到的结果。"""

    #: 用户的写法
    written: str
    #: 正式名
    title: str
    #: 是哪一类条目（站点插件认得的种类，如式神 / 御魂；其余是「页面」）
    kind: str
    #: wiki 上登记的别名
    aliases: tuple[str, ...] = ()
    #: 用户写的就是正式名或登记过的别名；False 表示只是名字里包含，不能当定论
    exact: bool = True


#: 查一个名字对应哪些条目（查不到返回空的）
NameLookup = Callable[[str], Awaitable[Sequence[NameNote]]]


@dataclass(slots=True)
class Understanding:
    """对问题的理解：模型的分析 + 从 wiki 数据查到的名字对照。"""

    topic: str = ""
    names: list[NameNote] = field(default_factory=list)
    time: str = ""
    #: 站内全文搜索用的搜索词（外号已经换成查到的正式名）；空的 = 没有可用的词，
    #: 第一级里的站内搜索就不查（等查证员看过资料再给词）
    keywords: str = ""

    def search_query(self, question: str) -> str:
        """检索用的问句：原话后面补上外号对应的正式名和具体时间。"""
        extra = [
            f"{note.written}即{note.title}"
            for note in self.names
            if note.exact and note.title.casefold() != note.written.casefold()
        ]
        if self.time and self.time not in question:
            extra.append(self.time)
        return f"{question}（{'；'.join(extra)}）" if extra else question

    def notes(self) -> list[str]:
        """告诉查证员和复核员的话，一条一句。"""
        lines: list[str] = []
        if self.topic:
            lines.append(f"这个问题问的是「{self.topic}」一类。")
        seen: set[tuple[str, str]] = set()
        for note in self.names:
            if not note.exact or (note.written, note.title) in seen:
                continue
            seen.add((note.written, note.title))
            if note.title.casefold() != note.written.casefold():
                lines.append(f"「{note.written}」是{note.kind}「{note.title}」的别名。")
        for written in dict.fromkeys(note.written for note in self.names if not note.exact):
            options = "、".join(
                f"{note.kind}「{note.title}」"
                for note in self.names
                if not note.exact and note.written == written
            )
            lines.append(f"wiki 上没有登记「{written}」这个叫法，名字里带它的有：{options}。")
        if self.time:
            lines.append(f"问题里说的时间指 {self.time}。")
        return lines


_PROMPT = """你在帮{intro} 的问答机器人做查资料前的准备。
读用户的问题，只做下面四件事，只返回 JSON：
{{"topic": "类别", "names": ["名字", …], "time": "具体时间", "keywords": ["关键词", …]}}

现在是 {now}。

1. topic：这句话主要在问哪一类，从这些里选一个最贴切的：{topics}。
2. names：问题里出现的名字，照用户的写法一字不差地摘出来——{names}。
   不要换成你认为的正式名，不要补全，不要解释；{generic}没有就给空列表。
3. time：问题里说到的相对时间换成具体的（照现在的时间算：「今年」→「{year}」，
   「上个月」→「{last_month}」，「昨天」→「{yesterday}」）；问题里本来就写了具体时间就照抄；
   「这次」「最近」「现在」这种说不出具体日期的，以及没提时间的，都给空字符串。
4. keywords：拿这个问题去 wiki 站内全文搜索该用的关键词，1～3 个，从问题里一字不差地摘：
   优先用第 2 项里的名字（最多两个），不够再加一个最能定位到内容的词；
   不要写整句，不要「什么时候」「怎么」「哪些」这类词；挑不出来就给空列表。

用户的问题：
{question}
"""


def _plain(text: str) -> str:
    """比较用：不分大小写，不管空白。"""
    return "".join(text.casefold().split())


def _prompt(
    question: str, topics: Sequence[str], timezone: tzinfo | None, wording: Wording | None = None
) -> str:
    wording = wording or Wording()
    today = datetime.now(timezone)
    last_month = today.replace(day=1) - timedelta(days=1)
    generic = "".join(f"「{word}」" for word in wording.generic_words)
    return _PROMPT.format(
        intro=wording.intro,
        names=wording.names,
        generic=f"{generic}这种泛指的词不算名字；" if generic else "",
        now=now_text(timezone),
        topics="、".join(topics),
        year=f"{today.year}年",
        last_month=f"{last_month.year}年{last_month.month}月",
        yesterday=f"{today - timedelta(days=1):%Y-%m-%d}",
        question=question,
    )


async def understand(
    llm: LLMClient,
    question: str,
    *,
    profile: str,
    lookup: NameLookup | None = None,
    topics: Sequence[str] = DEFAULT_TOPICS,
    timezone: tzinfo | None = None,
    timeout_seconds: float = 8.0,
    max_names: int = 5,
    wording: Wording | None = None,
) -> Understanding:
    """分析问题并查名字的别名。做不成的部分留空，不抛异常。"""
    try:
        analysis = await asyncio.wait_for(
            llm.chat_json(
                [user_message(_prompt(question, topics, timezone, wording))],
                Analysis,
                profile=profile,
            ),
            timeout_seconds,
        )
    except (LLMError, TimeoutError) as exc:
        logger.warning("research_analysis_failed", error=repr(exc)[:200])
        return Understanding()

    # 模型偶尔会摘出问题里根本没有的字眼（自作主张换成它以为的正式名）：只留原话里真有的
    plain = _plain(question)
    written = [
        name.strip()
        for name in dict.fromkeys(analysis.names)
        if _plain(name) and _plain(name) in plain
    ][:max_names]
    names: list[NameNote] = []
    if lookup is not None and written:
        results = await asyncio.gather(
            *(asyncio.wait_for(lookup(name), timeout_seconds) for name in written),
            return_exceptions=True,
        )
        for name, result in zip(written, results, strict=True):
            if isinstance(result, BaseException):
                logger.warning(
                    "research_name_lookup_failed", name_chars=len(name), error=repr(result)[:200]
                )
            else:
                names.extend(result)
    topic = analysis.topic.strip()
    understanding = Understanding(
        topic=topic if topic in topics else "",
        names=names,
        time=analysis.time.strip(),
        keywords=_site_keywords(analysis.keywords, plain, names),
    )
    # 只记数量：名字、搜索词都是从用户的问题里来的，不进日志（要看具体的，去处理记录里看这一步）
    logger.info(
        "research_understood",
        topic=understanding.topic,
        names_written=len(written),
        names_exact=sum(n.exact for n in names),
        names_guessed=sum(not n.exact for n in names),
        has_time=bool(understanding.time),
        keyword_chars=len(understanding.keywords),
    )
    return understanding


def _site_keywords(picked: Sequence[str], plain_question: str, names: Sequence[NameNote]) -> str:
    """站内搜索的搜索词：模型挑的词里只留问题里真有的，外号换成查到的正式名（站内搜索不认外号）。

    模型一个可用的词都没挑出来，就用查到的正式名（最多两个）。
    """
    official = {_plain(note.written): note.title for note in names if note.exact}
    words = [
        official.get(_plain(word), word.strip())
        for word in picked
        if _plain(word) and _plain(word) in plain_question
    ][:_MAX_KEYWORDS]
    if not words:
        words = list(official.values())[:2]
    return " ".join(dict.fromkeys(words))
