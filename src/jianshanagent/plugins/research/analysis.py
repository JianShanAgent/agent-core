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
import calendar
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, tzinfo
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from jianshanagent.core.llm import LLMClient, LLMContentBlocked, LLMError, user_message
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


@dataclass(frozen=True, slots=True)
class NewsSource:
    """站点注册的资讯来源；ID 与数据库平台字段分开，绝不接受自由查询条件。"""

    label: str
    aliases: tuple[str, ...]
    source_platform: str


class Analysis(BaseModel):
    """模型对问题的分析。"""

    topic: str = ""
    #: 问题里出现的名字，照用户的写法
    names: list[str] = Field(default_factory=list)
    #: 问题里的相对时间换成的具体时间；没有就是空的
    time: str = ""
    #: 站内全文搜索该用的关键词，照用户的写法
    keywords: list[str] = Field(default_factory=list)
    intent: Literal["lookup", "news_overview", "news_search"] = "lookup"
    #: 注册 ID；不是媒体词所对应的 topic，也不是自由 SQL 条件
    source: str = ""
    date_from: str | None = None
    date_to: str | None = None


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
    intent: str = "lookup"
    source: str = ""
    date_from: str | None = None
    date_to: str | None = None
    news_keywords: tuple[str, ...] = ()
    source_label: str = ""
    #: 本次请求的注册来源 ID 集合；source 仅保留用户明确指定的单一来源
    source_scope: tuple[str, ...] = ()
    date_error: str = ""
    analysis_failed: bool = False

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
        if self.intent != "lookup":
            lines.append(
                f"这是资讯{'概览' if self.intent == 'news_overview' else '主题检索'}，"
                f"限定来源：{self.source_label or self.source or '未明确'}。"
                f"公告发表日期范围：{self.date_from or '不限起日'} "
                f"至 {self.date_to or '资料截止日'}；"
                "未指定天数时取最近若干篇，不自行推定本月或近七天。"
            )
            lines.append(
                "新闻发布时间与活动举办时间是两回事；正文里的未来活动月份不能当作发帖日期。"
                "现在、已结束、即将开始必须按当前时间和正文核对，不清楚就不猜。"
                "知识库收录与联网补充要分别说明；未收录不能断言来源没有新动态。"
            )
            if self.date_error:
                lines.append(
                    f"发表日期范围解析失败：{self.date_error}。未执行资讯检索，"
                    "不能取消日期过滤后回答最新，也不能据此断言没有公告。"
                )
        elif self.topic:
            lines.append(f"这个问题问的是「{self.topic}」一类。")
        if self.analysis_failed and (self.intent != "lookup" or self.source_label):
            lines.append(
                "问题分析失败；只能使用明确的来源与检索结果，不能声称已经查明实时最新情况。"
            )
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
读用户的问题，做下面的分析，只返回 JSON：
{{"topic": "类别", "names": ["名字", …], "time": "具体时间", "keywords": ["关键词", …],
  "intent": "lookup/news_overview/news_search", "source": "注册ID",
  "date_from": null, "date_to": null}}

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

5. intent：普通查条目、技能、规则（即使有「最近」）用 lookup；问最近有哪些新动态、
   某资讯来源最近发了什么用 news_overview；找特定主题的消息/公告/帖子用 news_search。
   资讯不要求用户指定媒体；未指定时 source 留空，程序会限定到已注册来源集合。
   媒体词是来源，不是 topic；作为活动名/引号中名字的一部分时也不能当来源。
   source 只能选下列注册 ID；未限定来源留空；明确要求未注册来源时填 unknown，不得静默忽略：
   {news_sources}
6. date_from/date_to 只描述公告发表日期，ISO YYYY-MM-DD，含首尾。没有明确时间范围就 null，
   「最近/最新」不等于本月或七天。明确昨天、近一周、历史年月才换算。
   活动日期不是发表日期：「最近公布的12月活动」的12月属于主题，不能筛成12月发帖。
   news_overview 的 keywords 留空；news_search 只摘主题词，不要把来源别名当主题词。

用户的问题：
{question}
"""


def _plain(text: str) -> str:
    """比较用：不分大小写，不管空白。"""
    return "".join(text.casefold().split())


def _prompt(
    question: str,
    topics: Sequence[str],
    timezone: tzinfo | None,
    wording: Wording | None = None,
    news_sources: dict[str, NewsSource] | None = None,
    now: datetime | None = None,
) -> str:
    wording = wording or Wording()
    today = now or datetime.now(timezone or ZoneInfo("Asia/Shanghai"))
    last_month = today.replace(day=1) - timedelta(days=1)
    generic = "".join(f"「{word}」" for word in wording.generic_words)
    return _PROMPT.format(
        intro=wording.intro,
        names=wording.names,
        generic=f"{generic}这种泛指的词不算名字；" if generic else "",
        now=f"{today:%Y-%m-%d %H:%M %Z}",
        news_sources="；".join(
            f"{key}：{source.label}（别名：{'、'.join(source.aliases)}）"
            for key, source in (news_sources or {}).items()
        )
        or "（尚未注册任何来源）",
        topics="、".join(topics),
        year=f"{today.year}年",
        last_month=f"{last_month.year}年{last_month.month}月",
        yesterday=f"{today - timedelta(days=1):%Y-%m-%d}",
        question=question,
    )


def _unquoted(question: str) -> str:
    return re.sub(r'「[^」]*」|《[^》]*》|“[^”]*”|"[^"]*"', "", question)


def _explicit_sources(question: str, sources: dict[str, NewsSource]) -> tuple[str, ...]:
    plain = _plain(_unquoted(question))
    matched = [
        key
        for key, source in sources.items()
        if any(
            _plain(alias) in plain
            and not re.search(re.escape(_plain(alias)) + r"(?:之夜|庆典|大会|杯)", plain)
            for alias in (source.label, *source.aliases)
            if alias
        )
    ]
    return tuple(matched)


def _explicit_source(question: str, sources: dict[str, NewsSource]) -> str:
    matched = _explicit_sources(question, sources)
    return matched[0] if len(matched) == 1 else ""


def _safe_overview(question: str, sources: dict[str, NewsSource]) -> str:
    """模型失败或旧输出的安全后备；不把普通「最近技能」或活动名劫持成资讯。"""
    source = _explicit_source(question, sources)
    if not source:
        return ""
    # 主题即使被引号包住也必须保留；删除它会把主题新闻误判成泛来源概览。
    text = _plain(question)
    for alias in sorted((sources[source].label, *sources[source].aliases), key=len, reverse=True):
        text = text.replace(_plain(alias), "")
    if re.fullmatch(
        r"[\s，,？?。]*(?:最近|最新|近期|这几天)[\s，,的上里]*"
        r"(?:有什么新(?:东西|消息|动态|内容)?|有什么(?:消息|动态|更新|公告)|"
        r"(?:发|发布|公布)了什么|(?:消息|动态|更新|公告)(?:有哪些|有什么)?)"
        r"[\s吗呢呀啊，,？?。]*",
        text,
    ):
        return source
    return ""


# 只识别有界日期 token；其余明确时间交给经过校验的结构化结果，不把任意大整数转成天数。
_CALENDAR_TOKEN = re.compile(
    r"(?<!\d)(?:(?P<iso>\d{4}-\d{2}(?:-\d{2})?)|"
    r"(?:(?P<year>\d{4})年)?(?P<month>\d{1,2})月(?:(?P<day>\d{1,2})[日号]?)?|"
    r"(?P<year_only>\d{4})年)(?!\d)"
)
_MAX_DATE_QUESTION = 8192
_MAX_RELATIVE_DAYS = 36600


def _publication_bounds(
    question: str,
    today: date,
    analysis: Analysis,
) -> tuple[str | None, str | None, str]:
    """原话提供时间证据才接受区间；无效/无法确认的区间显式失败，不悄悄撤掉过滤。"""
    if len(question) > _MAX_DATE_QUESTION:
        return None, None, "问题过长，无法安全确认发表日期范围"
    text = _unquoted(question)
    # 只移除确实修饰活动的日期，不截断「发布的」后面可能存在的发表范围。
    activity_date = (
        r"(?<!\d)(?:\d{4}年)?\d{1,2}月(?:\d{1,2}[日号]?)?"
        r"(?=(?:的)?(?:活动|上线|举办|开始|结束|开幕))"
    )
    text = re.sub(activity_date, "", text)
    try:
        relative = re.search(r"(?:近|过去|最近)([0-9]+|[一二三四五六七八九十百]+)(?:天|日)", text)
        weeks = re.search(r"(?:近|最近|过去)(?:一|1)?周", text)
        if relative:
            value = relative[1]
            if len(value) > 5:
                raise ValueError("相对天数超出可查询范围")
            if value.isdigit():
                days = int(value)
            else:
                number = {
                    "一": 1,
                    "二": 2,
                    "三": 3,
                    "四": 4,
                    "五": 5,
                    "六": 6,
                    "七": 7,
                    "八": 8,
                    "九": 9,
                    "十": 10,
                }
                if value in number:
                    days = number[value]
                elif value.count("十") == 1 and len(value) <= 3:
                    tens, units = value.split("十")
                    days = (number[tens] if tens else 1) * 10 + (number[units] if units else 0)
                else:
                    raise ValueError("相对天数无法确认")
            if not 1 <= days <= _MAX_RELATIVE_DAYS:
                raise ValueError("相对天数超出可查询范围")
            return (today - timedelta(days=days - 1)).isoformat(), today.isoformat(), ""
        if weeks:
            return (today - timedelta(days=6)).isoformat(), today.isoformat(), ""
        if "昨天" in text:
            day = (today - timedelta(days=1)).isoformat()
            return day, day, ""
        if "今天" in text or "当天" in text:
            return today.isoformat(), today.isoformat(), ""
        if "上个月" in text:
            previous = today.replace(day=1) - timedelta(days=1)
            return previous.replace(day=1).isoformat(), previous.isoformat(), ""
        if "本月" in text or "这个月" in text:
            return today.replace(day=1).isoformat(), today.isoformat(), ""
        for value, unit in re.findall(r"(?<!\d)(\d+)(年|月|日|号)", text):
            if len(value) > (4 if unit == "年" else 2):
                raise ValueError("日期数值超长")
            number_value = int(value)
            if (
                unit == "年"
                and not 1 <= number_value <= 9999
                or unit == "月"
                and not 1 <= number_value <= 12
                or unit in {"日", "号"}
                and not 1 <= number_value <= 31
            ):
                raise ValueError("日期数值无效")
        for iso_day in re.findall(r"(?<!\d)\d{4}-\d+-\d+(?!\d)", text):
            date.fromisoformat(iso_day)
        tokens = list(_CALENDAR_TOKEN.finditer(text))
        if len(tokens) > 2:
            raise ValueError("发表日期存在多个范围，无法确认")
        periods: list[tuple[date, date]] = []
        for token in tokens:
            explicit_year = bool(token["year"] or token["iso"] or token["year_only"])
            if token["iso"]:
                parts = token["iso"].split("-")
                year, month = int(parts[0]), int(parts[1])
                day_number = int(parts[2]) if len(parts) == 3 else None
            elif token["year_only"]:
                year = int(token["year_only"])
                periods.append((date(year, 1, 1), date(year, 12, 31)))
                continue
            else:
                year = int(token["year"] or (periods[-1][0].year if periods else today.year))
                prefix = text[max(0, token.start() - 2) : token.start()]
                relative_years = {"今年": 0, "去年": -1, "前年": -2, "明年": 1}
                if prefix in relative_years:
                    year = today.year + relative_years[prefix]
                    explicit_year = True
                elif prefix == "次年" and periods:
                    year = periods[-1][0].year + 1
                    explicit_year = True
                month = int(token["month"])
                day_number = int(token["day"]) if token["day"] else None
            start = date(year, month, day_number or 1)
            if periods and not explicit_year and start < periods[-1][0]:
                year += 1  # 11月到1月：后端省略年份时继承并跨年，不缩成首月。
                start = date(year, month, day_number or 1)
            end = date(year, month, day_number or calendar.monthrange(year, month)[1])
            periods.append((start, end))
        if periods:
            if len(periods) == 2:
                between = text[tokens[0].end() : tokens[1].start()]
                if not re.search(r"到|至|[-—~～]", between):
                    raise ValueError("多个发表日期没有明确区间关系")
                if periods[1][1] < periods[0][0]:
                    raise ValueError("发表日期终点早于起点")
            return periods[0][0].isoformat(), periods[-1][1].isoformat(), ""
        explicit_time = bool(
            re.search(
                r"\d+(?:年|月|日|号|天)|\d{4}-|去年|今年|上周|这周|本周|近一月|近一个月", text
            )
        )
        if not explicit_time:
            return None, None, ""  # 「最近/最新」绝不是允许模型凭空指定本月的证据。
        if analysis.date_from or analysis.date_to:
            lower = date.fromisoformat(analysis.date_from) if analysis.date_from else None
            upper = date.fromisoformat(analysis.date_to) if analysis.date_to else None
            if lower and upper and upper < lower:
                raise ValueError("发表日期终点早于起点")
            return lower.isoformat() if lower else None, upper.isoformat() if upper else None, ""
        raise ValueError("明确发表时间无法换算为可靠日期范围")
    except (ValueError, OverflowError, KeyError):
        return None, None, "发表日期无效、区间倒置或超出安全查询范围"


def _news_fields(
    analysis: Analysis,
    question: str,
    sources: dict[str, NewsSource],
    today: date,
    *,
    failed: bool = False,
) -> dict[str, Any]:
    # 正确的主题分析优先；后备只补旧模型/失败的 lookup，不覆盖 news_search。
    safe = _safe_overview(question, sources) if analysis.intent == "lookup" else ""
    requested_source = analysis.source.strip()
    explicit = _explicit_sources(question, sources)
    intent = "news_overview" if safe else analysis.intent
    source_id = safe or (explicit[0] if len(explicit) == 1 else "")
    unsupported = bool(requested_source and requested_source not in sources)
    if unsupported and not safe:
        source_id = requested_source
    # 明确资讯但未指定媒体：查询所有注册来源，而非全库相似度，也不接受模型自行缩窄来源。
    scope = () if unsupported and not safe else explicit or tuple(sources)
    if failed and not safe:
        intent = "lookup"
    if (
        re.search(r"技能|机制|属性|数值", question)
        and re.search(r"怎么|如何|是什么|怎么算", question)
        or re.search(r"怎么打|怎么玩|如何玩|如何打", question)
    ) and not re.search(r"发布|公布|公告|消息|动态|更新|帖子", question):
        intent = "lookup"
    if intent == "lookup":
        explicit_id = _explicit_source(question, sources)
        uncertain_source = (
            sources[explicit_id].label
            if failed and explicit_id and re.search(r"最近|最新|近期|新消息", question)
            else ""
        )
        return {"analysis_failed": failed, "source_label": uncertain_source}
    lower, upper, date_error = _publication_bounds(question, today, analysis)
    aliases = {
        _plain(alias) for key in scope for alias in (sources[key].label, *sources[key].aliases)
    }
    keywords = (
        tuple(
            dict.fromkeys(
                word.strip()
                for word in analysis.keywords
                if _plain(word) and _plain(word) in _plain(question) and _plain(word) not in aliases
            )
        )[:_MAX_KEYWORDS]
        if intent == "news_search"
        else ()
    )
    return dict(
        intent=intent,
        source=source_id,
        source_label="、".join(sources[key].label for key in scope),
        source_scope=scope,
        date_error=date_error,
        date_from=lower,
        date_to=upper,
        news_keywords=keywords,
        analysis_failed=failed,
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
    news_sources: dict[str, NewsSource] | None = None,
    now: datetime | None = None,
) -> Understanding:
    """分析问题并查名字的别名。普通失败留空，内容审核拦截原样传给调用方。"""
    sources = news_sources or {}
    current = now or datetime.now(timezone or ZoneInfo("Asia/Shanghai"))
    try:
        analysis = await llm.chat_json(
            [user_message(_prompt(question, topics, timezone, wording, sources, current))],
            Analysis,
            profile=profile,
            deadline=asyncio.get_running_loop().time() + timeout_seconds,
        )
    except LLMContentBlocked:
        raise
    except (LLMError, TimeoutError) as exc:
        logger.warning("research_analysis_failed", error=repr(exc)[:200])
        return Understanding(
            **_news_fields(Analysis(), question, sources, current.date(), failed=True)
        )

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
        **_news_fields(analysis, question, sources, current.date()),
    )
    if understanding.intent != "lookup":
        # 来源概览不让错误分类与来源名对应的条目检索偏置作答。
        understanding.topic = ""
        if understanding.intent == "news_overview":
            understanding.names = []
        understanding.keywords = " ".join(understanding.news_keywords)
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
