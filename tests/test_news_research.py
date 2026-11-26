"""近期资讯：固定上海时钟、纯内存知识库与假模型，不访问外部服务。"""

import asyncio
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from jianshanagent.core import trace
from jianshanagent.core.llm import LLMError
from jianshanagent.plugins.knowledge.news import NewsQuery, NewsResult
from jianshanagent.plugins.knowledge.retrieval import Retriever
from jianshanagent.plugins.knowledge.store import KbChunk, MemoryKnowledgeStore, scoped
from jianshanagent.plugins.research.analysis import Analysis, NameNote, NewsSource, understand
from jianshanagent.plugins.research.evidence import Evidence, KnowledgeSource, PageFiller
from jianshanagent.plugins.research.workflow import Decision, ResearchWorkflow, WorkflowSettings
from test_research import FakeSource, ScriptedLLM, ev

NOW = datetime(2026, 11, 26, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
SOURCES = {
    "weibo": NewsSource("微博", ("微博", "官博", "官方微博", "扫地工微博"), "weibo"),
    "board": NewsSource("公告板", ("公告板",), "board"),
}


def chunk(
    key: str,
    day: str | None,
    title: str,
    text: str = "",
    *,
    platform: str = "weibo",
    wiki: str = "",
    seq: int = 0,
    doc: str | None = None,
) -> KbChunk:
    return KbChunk(
        chunk_id=scoped(wiki, key),
        doc_key=scoped(wiki, doc or key),
        kind="announcement",
        title=title,
        text=text or title,
        date=day,
        wiki=wiki,
        seq=seq,
        meta={"source_platform": platform},
    )


async def knowledge(*chunks: KbChunk, excluded: tuple[str, ...] = ()) -> KnowledgeSource:
    store = MemoryKnowledgeStore()
    for wiki in sorted({part.wiki for part in chunks}):
        await store.sync(
            [(part, None) for part in chunks if part.wiki == wiki],
            fingerprint="test",
            wiki=wiki,
        )
    return KnowledgeSource(Retriever(store, None, exclude_wikis=excluded))


def allowed_platforms(query: NewsQuery) -> tuple[str, ...]:
    filters = query.filters()
    assert not filters.allows(chunk("other", "2026-11-24", "其它来源", platform="other"))
    return tuple(
        platform
        for platform in SOURCES
        if filters.allows(chunk(platform, "2026-11-24", platform, platform=platform))
    )


def flow(llm: ScriptedLLM, *levels: Any, **settings: Any) -> ResearchWorkflow:
    return ResearchWorkflow(
        llm,
        levels,
        WorkflowSettings(
            analysis_profile="quick", news_sources=settings.pop("news_sources", SOURCES), **settings
        ),
        clock=lambda: NOW,
    )


async def analyze(question: str, analysis: Analysis | Exception) -> Any:
    return await understand(
        ScriptedLLM([], analysis=analysis),
        question,
        profile="quick",
        news_sources=SOURCES,
        now=NOW,
    )


async def test_original_trace_uses_november_documents_without_vector_or_web() -> None:
    kb = await knowledge(
        chunk("old", "2018-12-01", "旧同人新闻"),
        chunk("a", "2026-11-24", "11月25日维护更新公告"),
        chunk("b", "2026-11-24", "「狼影云结」活动12月2日上线"),
        chunk("c", "2026-11-23", "十二月花合战上新"),
        chunk("future", "2026-12-01", "未来发表，不可读取"),
    )
    web = FakeSource("web", "联网搜索", [])
    site = FakeSource("site", "站内搜索", [ev("noise", "2018年的噪声")])
    llm = ScriptedLLM(
        [Decision(answer="11月24日公布：12月2日上线狼影云结。", sources=[2])],
        analysis=Analysis(topic="周边同人", keywords=["微博"]),
    )
    outcome = await flow(llm, [kb, site], web).run("最近微博有什么新东西吗")
    assert [item.key for item in outcome.evidence] == ["a", "b", "c", "old"]
    assert site.asked == web.asked == []
    for prompt in (llm.analyst_prompts[0], llm.review_prompts[0]):
        assert "周边同人" not in prompt
        assert "知识库实际来源：微博（平台 weibo）" in prompt
        assert "资料截止日 2026-11-26" in prompt
        assert "最新可靠发表日 2026-11-24" in prompt
        assert "未超过陈旧阈值" in prompt
        assert "新闻发布时间与活动举办时间" in prompt
        assert "现在是 2026-11-26" in prompt


@pytest.mark.parametrize(
    ("question", "analysis", "bounds", "keywords"),
    [
        (
            "最近微博有什么新东西吗",
            Analysis(intent="news_overview", source="weibo"),
            (None, None),
            (),
        ),
        (
            "微博2025年11月发布了什么",
            Analysis(intent="news_overview", source="weibo"),
            ("2025-11-01", "2025-11-30"),
            (),
        ),
        (
            "昨天官博发了什么",
            Analysis(intent="news_overview", source="weibo"),
            ("2026-11-25", "2026-11-25"),
            (),
        ),
        (
            "微博近一周有哪些公告",
            Analysis(intent="news_overview", source="weibo"),
            ("2026-11-20", "2026-11-26"),
            (),
        ),
        (
            "微博最近公布的12月活动",
            Analysis(
                intent="news_search",
                source="weibo",
                date_from="2026-12-01",
                date_to="2026-12-31",
                keywords=["微博", "12月", "活动"],
            ),
            (None, None),
            ("12月", "活动"),
        ),
        (
            "最近官博关于花合战的消息",
            Analysis(
                intent="news_search", source="weibo", keywords=["官博", "花合战", "不存在的词"]
            ),
            (None, None),
            ("花合战",),
        ),
    ],
)
async def test_publication_bounds_and_source_keywords(
    question: str,
    analysis: Analysis,
    bounds: tuple[str | None, str | None],
    keywords: tuple[str, ...],
) -> None:
    result = await analyze(question, analysis)
    assert (result.date_from, result.date_to) == bounds
    assert result.news_keywords == keywords
    assert result.source == "weibo"


async def test_topic_matches_document_body_not_just_first_chunk_and_fills_same_doc_only() -> None:
    kb = await knowledge(
        chunk("now0", "2026-11-24", "维护更新公告", "常规维护", seq=0, doc="now"),
        chunk("now1", "2026-11-24", "维护更新公告", "12月活动：狼影云结", seq=1, doc="now"),
        chunk("old0", "2025-11-24", "维护更新公告", "旧年份活动", doc="old"),
    )
    llm = ScriptedLLM(
        [Decision(answer="11月24日预告12月活动。", sources=[2])],
        analysis=Analysis(intent="news_search", source="weibo", keywords=["12月", "活动"]),
    )
    workflow = flow(llm, kb)
    workflow.page_filler = PageFiller(kb.retriever, min_hits=2)
    result = await workflow.run("微博最近公布的12月活动")
    assert [item.key for item in result.evidence] == ["now0", "now1"]
    assert {item.doc_key for item in result.evidence} == {"now"}
    assert "旧年份活动" not in llm.analyst_prompts[0]


@pytest.mark.parametrize("parts", [[], [chunk("old", "2025-11-20", "旧消息")]])
async def test_empty_or_stale_kb_can_escalate_with_explicit_reason(
    parts: list[KbChunk],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records: list[dict[str, Any]] = []
    original = trace.Step.set

    def capture(self: Any, **values: Any) -> None:
        records.append(values)
        original(self, **values)

    monkeypatch.setattr(trace.Step, "set", capture)
    kb = await knowledge(*parts)
    web = FakeSource("web", "联网搜索", [ev("web", "当天核实的新消息", "联网搜索")])
    llm = ScriptedLLM(
        [
            Decision(action="search", keywords="微博 最新公告 2026年11月26日"),
            Decision(answer="网上补充查到当天的新消息。", sources=[len(parts) + 1]),
        ],
        analysis=Analysis(intent="news_overview", source="weibo"),
    )
    result = await flow(llm, kb, web).run("最近微博有什么新东西吗")
    assert web.asked and result.steps == ["analyze", "kb", "web", "review"]
    assert any("reason" in record for record in records)
    assert any(record.get("mode") == "news_overview" for record in records)
    first = llm.analyst_prompts[0]
    assert ("本次返回 0 篇" if not parts else "陈旧/新鲜度未知") in first
    assert "未收录不能断言来源没有新动态" in first
    assert "知识库收录与联网补充要分别说明" in first


async def test_analysis_failure_only_uses_safe_unambiguous_source_overview() -> None:
    result = await analyze("最近微博有什么新东西吗", LLMError("offline"))
    assert result.intent == "news_overview" and result.source == "weibo"
    assert result.analysis_failed
    assert "不能声称已经查明实时最新情况" in " ".join(result.notes())
    kb = await knowledge(chunk("new", "2026-11-24", "新公告"))
    llm = ScriptedLLM([Decision(answer="库里11月24日有新公告。")], analysis=LLMError("offline"))
    outcome = await flow(llm, kb).run("最近微博有什么新东西吗")
    assert [item.key for item in outcome.evidence] == ["new"]
    for question in ("最近技能改了吗", "最近微博说的技能怎么用", "最近公告板活动怎么打"):
        conservative = await analyze(question, LLMError("offline"))
        assert conservative.intent == "lookup"


@pytest.mark.parametrize(
    "question",
    [
        "最近技能是什么",
        "最近「微博之夜」活动怎么打",
        "最近微博之夜怎么玩",
        "最近微博提到的技能怎么用",
    ],
)
async def test_recent_lookup_and_media_words_in_activity_titles_are_not_news(question: str) -> None:
    result = await analyze(question, Analysis(intent="news_search", source="weibo"))
    assert result.intent == "lookup"


async def test_unknown_source_is_explicitly_unsupported_and_never_unfiltered() -> None:
    kb = FakeSource("kb", "知识库", [ev("wrong", "其它来源新闻")])
    llm = ScriptedLLM(
        [Decision(answer="这个资讯来源目前不受支持。")],
        analysis=Analysis(intent="news_overview", source="unknown"),
    )
    result = await flow(llm, kb).run("最近未注册平台发了什么新闻")
    assert not kb.asked and not result.evidence
    assert "来源未注册或不受支持" in llm.analyst_prompts[0]


async def test_shared_source_does_not_leak_filters_between_concurrent_requests() -> None:
    seen: list[NewsQuery] = []

    class SharedKnowledge:
        name, label, keywords_hint = "kb", "知识库", ""

        async def collect_news(self, query: NewsQuery) -> tuple[list[Evidence], NewsResult]:
            seen.append(query)
            await asyncio.sleep(0)
            platforms = allowed_platforms(query)
            assert len(platforms) == 1
            key = platforms[0]
            item = ev(key, key)
            return [item], NewsResult([], "2026-11-24", 1, 0)

    shared = SharedKnowledge()
    one = ScriptedLLM(
        [Decision(answer="a")],
        analysis=Analysis(
            intent="news_search",
            source="weibo",
            keywords=["花合战"],
        ),
    )
    two = ScriptedLLM(
        [Decision(answer="b")],
        analysis=Analysis(
            intent="news_search",
            source="board",
            keywords=["维护"],
        ),
    )
    a, b = await asyncio.gather(
        flow(one, shared).run("昨天微博发的花合战消息"),
        flow(two, shared).run("公告板2025年11月的维护公告"),
    )
    assert [item.key for item in a.evidence] == ["weibo"]
    assert [item.key for item in b.evidence] == ["board"]
    assert {(allowed_platforms(q), q.date_from, q.keywords) for q in seen} == {
        (("weibo",), "2026-11-25", ("花合战",)),
        (("board",), "2025-11-01", ("维护",)),
    }
    assert set(vars(shared)) == set()


async def test_news_filling_obeys_budget_and_excludes_unmounted_wikis() -> None:
    parts = [
        chunk("main0", "2026-11-24", "同名公告", "首片", doc="main-doc"),
        chunk("main1", "2026-11-24", "同名公告", "后片" * 100, doc="main-doc", seq=1),
        chunk("gone", "2026-11-26", "同名公告", "卸载 wiki", wiki="removed"),
        chunk("other", "2026-11-25", "同名公告", "其它平台", platform="board"),
    ]
    kb = await knowledge(*parts, excluded=("removed",))
    items, result = await kb.collect_news(NewsQuery(("weibo",), as_of="2026-11-26"))
    assert [item.key for item in items] == ["main0"]
    assert result.source_latest_date == "2026-11-24"
    filler = PageFiller(kb.retriever, max_chars=1000)
    tight = await filler.fill_news(items, 0, 1)
    assert [item.key for item in tight] == ["main0"] and tight[0].excerpt
    full = await filler.fill_news(items, 0, 2000)
    assert [item.key for item in full] == ["main0", "main1"]


@pytest.mark.parametrize("source", ["微博", "官博", "官方微博", "扫地工微博"])
async def test_failure_fallback_uses_registered_aliases_longest_first(source: str) -> None:
    result = await analyze(f"最近{source}有什么新东西吗", LLMError("offline"))
    assert result.intent == "news_overview" and result.source == "weibo"


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("微博11月24日的公告", ("2026-11-24", "2026-11-24")),
        ("微博2025年11月24日的公告", ("2025-11-24", "2025-11-24")),
        ("微博发布于2025年11月的消息", ("2025-11-01", "2025-11-30")),
        ("微博最近的12月活动有什么消息", (None, None)),
        ("微博最近公布12月活动", (None, None)),
    ],
)
async def test_explicit_publication_days_and_activity_months(
    question: str,
    expected: tuple[str | None, str | None],
) -> None:
    result = await analyze(question, Analysis(intent="news_search", source="weibo"))
    assert (result.date_from, result.date_to) == expected


async def test_latest_count_is_clamped_and_history_window_is_preserved() -> None:
    kb = await knowledge(
        *(chunk(str(i), "2026-11-24", f"新公告{i}") for i in range(8)),
        chunk("history", "2025-11-10", "历史公告"),
    )
    llm = ScriptedLLM(
        [Decision(answer="最近公告")],
        analysis=Analysis(
            intent="news_overview",
            source="weibo",
        ),
    )
    result = await flow(llm, kb, news_latest_documents=8, news_max_documents=3).run(
        "最近微博有什么新东西吗"
    )
    assert len(result.evidence) == 3
    other = ScriptedLLM(
        [Decision(answer="历史公告")],
        analysis=Analysis(
            intent="news_overview",
            source="weibo",
        ),
    )
    history = await flow(other, kb).run("微博2025年11月有哪些公告")
    assert [item.key for item in history.evidence] == ["history"]
    assert "来源全库最新可靠发表日 2026-11-24" in other.analyst_prompts[0]


async def test_quoted_topic_is_never_erased_by_generic_overview_fallback() -> None:
    question = "最近微博有什么「花合战」新消息吗"
    result = await analyze(
        question,
        Analysis(
            intent="news_search",
            source="weibo",
            keywords=["花合战"],
        ),
    )
    assert result.intent == "news_search" and result.news_keywords == ("花合战",)
    failed = await analyze(question, LLMError("offline"))
    assert failed.intent == "lookup"  # 不删掉引号主题来伪造一个无主题的安全概览。
    kb = await knowledge(
        chunk("new-other", "2026-11-26", "无关更新"),
        chunk("topic", "2026-11-24", "花合战上新"),
    )
    llm = ScriptedLLM(
        [Decision(answer="花合战上新")],
        analysis=Analysis(
            intent="news_search",
            source="weibo",
            keywords=["花合战"],
        ),
    )
    outcome = await flow(llm, kb).run(question)
    assert [item.key for item in outcome.evidence] == ["topic"]


@pytest.mark.parametrize(
    ("question", "lower", "upper"),
    [
        ("微博发布的最近一周公告有哪些", "2026-11-20", "2026-11-26"),
        ("微博公布了最近一周的公告有哪些", "2026-11-20", "2026-11-26"),
        ("微博发布的2025年11月公告有哪些", "2025-11-01", "2025-11-30"),
        ("微博2025年11月到2026年1月发布了哪些公告", "2025-11-01", "2026-01-31"),
        ("微博2025年11月到1月发布了哪些公告", "2025-11-01", "2026-01-31"),
        ("微博11月至1月发布了哪些公告", "2026-11-01", "2027-01-31"),
        ("微博去年11月到今年1月发布了哪些公告", "2025-11-01", "2026-01-31"),
        ("微博2025年11月到次年1月发布了哪些公告", "2025-11-01", "2026-01-31"),
        ("微博2025-11到2026-01发布了哪些公告", "2025-11-01", "2026-01-31"),
        ("微博2025年11月24日到2026年1月3日发布了哪些公告", "2025-11-24", "2026-01-03"),
    ],
)
async def test_publication_suffix_and_complete_month_ranges(
    question: str,
    lower: str,
    upper: str,
) -> None:
    for use_model_bounds in (False, True):
        result = await analyze(
            question,
            Analysis(
                intent="news_overview",
                source="weibo",
                date_from=lower if use_model_bounds else None,
                date_to=upper if use_model_bounds else None,
            ),
        )
        assert (result.date_from, result.date_to) == (lower, upper)
        assert not result.date_error


async def test_cross_year_query_keeps_documents_through_end_month() -> None:
    kb = await knowledge(
        chunk("nov", "2025-11-01", "十一月公告"),
        chunk("dec", "2025-12-24", "十二月公告"),
        chunk("jan", "2026-01-31", "一月公告"),
        chunk("excluded", "2026-02-01", "二月公告"),
    )
    llm = ScriptedLLM(
        [Decision(answer="三个月的公告")],
        analysis=Analysis(
            intent="news_overview",
            source="weibo",
        ),
    )
    result = await flow(llm, kb).run("微博2025年11月到1月发布了哪些公告")
    assert [item.key for item in result.evidence] == ["jan", "dec", "nov"]


@pytest.mark.parametrize("model_source", ["", "weibo"])
@pytest.mark.parametrize(
    ("question", "intent", "keywords"),
    [
        ("最近神乐皮肤有什么消息", "news_search", ["神乐", "皮肤"]),
        ("最近有哪些活动新动态", "news_overview", []),
    ],
)
async def test_news_without_explicit_media_uses_registered_source_set(
    model_source: str,
    question: str,
    intent: str,
    keywords: list[str],
) -> None:
    seen: list[NewsQuery] = []

    class ScopedKnowledge:
        name, label, keywords_hint = "kb", "知识库", ""

        async def collect(self, *args: Any) -> list[Evidence]:
            raise AssertionError("资讯不能退回普通相关性检索")

        async def collect_news(self, query: NewsQuery) -> tuple[list[Evidence], NewsResult]:
            seen.append(query)
            return [], NewsResult([])

    llm = ScriptedLLM(
        [Decision(answer="库里没有匹配的收录。")],
        analysis=Analysis(
            intent=intent,
            source=model_source,
            keywords=keywords,
        ),
    )
    await flow(llm, ScopedKnowledge()).run(question)
    assert allowed_platforms(seen[0]) == ("weibo", "board")
    assert seen[0].keywords == tuple(keywords)
    one = ScriptedLLM(
        [Decision(answer="库里没有匹配的收录。")],
        analysis=Analysis(
            intent=intent,
            source=model_source,
            keywords=keywords,
        ),
    )
    await flow(one, ScopedKnowledge(), news_sources={"weibo": SOURCES["weibo"]}).run(question)
    assert allowed_platforms(seen[1]) == ("weibo",)


async def test_plain_recent_character_skill_stays_lookup_without_media() -> None:
    result = await analyze(
        "最近常用神乐她技能是什么",
        Analysis(
            intent="news_search",
            source="weibo",
            names=["神乐"],
            keywords=["神乐"],
        ),
    )
    assert result.intent == "lookup" and not result.source_scope


@pytest.mark.parametrize(
    "question",
    [
        "微博近999999999999999999999999999999天有哪些公告",
        "微博近0天有哪些公告",
        "微博2026年2月30日有哪些公告",
        "微博2026-02-30有哪些公告",
        "微博2026年13月有哪些公告",
        "微博0000年11月有哪些公告",
        "微博999999年11月有哪些公告",
        "微博2026年11月到2025年1月有哪些公告",
        "微博2026-11-24到2026-11-20有哪些公告",
        "微博近" + "9" * 10000 + "天有哪些公告",
    ],
    ids=[
        "huge-days",
        "zero-days",
        "bad-chinese-day",
        "bad-iso-day",
        "bad-month",
        "year-zero",
        "huge-year",
        "reversed-months",
        "reversed-days",
        "huge-input",
    ],
)
async def test_invalid_or_unbounded_dates_fail_closed_without_api_calls(question: str) -> None:
    analysis = Analysis(intent="news_overview", source="weibo")
    understanding = await analyze(question, analysis)
    assert understanding.date_error
    assert "未执行资讯检索" in " ".join(understanding.notes())
    kb, web = FakeSource("kb", "知识库", []), FakeSource("web", "联网搜索", [])
    llm = ScriptedLLM([], analysis=analysis)
    outcome = await flow(llm, kb, web).run(question)
    assert "无法确认" in outcome.answer and "未执行资讯检索" in outcome.answer
    assert not kb.asked and not web.asked and not llm.analyst_prompts


async def test_model_bounds_require_explicit_publication_evidence() -> None:
    guessed = await analyze(
        "最近微博有什么新东西吗",
        Analysis(
            intent="news_overview",
            source="weibo",
            date_from="2026-11-01",
            date_to="2026-11-30",
        ),
    )
    assert (guessed.date_from, guessed.date_to) == (None, None) and not guessed.date_error
    activity = await analyze(
        "最近微博公布的12月活动有什么消息",
        Analysis(
            intent="news_search",
            source="weibo",
            keywords=["12月", "活动"],
            date_from="2026-12-01",
            date_to="2026-12-31",
        ),
    )
    assert (activity.date_from, activity.date_to) == (None, None) and not activity.date_error
    invalid = await analyze(
        "微博去年有什么公告",
        Analysis(
            intent="news_overview",
            source="weibo",
            date_from="not-a-date",
        ),
    )
    assert invalid.date_error
    structured = await analyze(
        "微博去年有什么公告",
        Analysis(
            intent="news_overview",
            source="weibo",
            date_from="2025-01-01",
            date_to="2025-12-31",
        ),
    )
    assert (structured.date_from, structured.date_to) == ("2025-01-01", "2025-12-31")


async def test_news_never_takes_unfiltered_entry_pages_even_for_exact_names() -> None:
    lookup_calls: list[str] = []

    async def lookup(name: str) -> list[NameNote]:
        lookup_calls.append(name)
        return [NameNote(name, name, "条目")]

    entry = FakeSource("entry", "普通条目页", [ev("entry", "普通技能页" * 100)])
    kb = await knowledge(chunk("topic", "2026-11-24", "神乐皮肤新消息"))
    llm = ScriptedLLM(
        [Decision(answer="有皮肤新消息")],
        analysis=Analysis(
            intent="news_search",
            source="weibo",
            names=["神乐"],
            keywords=["神乐", "皮肤"],
        ),
    )
    workflow = flow(llm, kb, evidence_chars=20)
    workflow.name_lookup, workflow.entry_pages = lookup, entry
    news = await workflow.run("微博最近神乐皮肤有什么消息")
    assert lookup_calls == ["神乐"] and not entry.asked
    assert [item.key for item in news.evidence] == ["topic"]
    assert "普通技能页" not in llm.analyst_prompts[0]
    ordinary = ScriptedLLM(
        [Decision(answer="普通技能")],
        analysis=Analysis(
            names=["神乐"],
            keywords=["神乐"],
        ),
    )
    plain = flow(ordinary, FakeSource("kb", "知识库", []))
    plain.name_lookup, plain.entry_pages = lookup, entry
    await plain.run("神乐技能是什么")
    assert entry.asked and lookup_calls == ["神乐", "神乐"]


async def test_recent_source_does_not_hide_stale_topic_and_history_remains_useful() -> None:
    kb = await knowledge(
        chunk("new", "2026-11-24", "无关的新公告"),
        chunk("old-topic", "2024-01-15", "神乐皮肤消息"),
    )
    web = FakeSource("web", "联网搜索", [ev("new-topic", "网上补充的新消息", "联网搜索")])
    llm = ScriptedLLM(
        [
            Decision(action="search", keywords="神乐 皮肤 最新消息"),
            Decision(answer="网上补充的新消息", sources=[2]),
        ],
        analysis=Analysis(intent="news_search", source="weibo", keywords=["神乐", "皮肤"]),
    )
    await flow(llm, kb, web).run("微博最近神乐皮肤有什么消息")
    first = llm.analyst_prompts[0]
    assert "来源全库最新可靠发表日 2026-11-24" in first
    assert "本次命中最新发表日 2024-01-15" in first
    assert "陈旧/新鲜度未知" in first and "需要核实补充" in first
    assert "知识库已有近期资料" not in first and web.asked
    historical_web = FakeSource("web", "联网搜索", [])
    old = ScriptedLLM(
        [Decision(answer="2024年1月有皮肤消息")],
        analysis=Analysis(
            intent="news_search",
            source="weibo",
            keywords=["神乐", "皮肤"],
        ),
    )
    await flow(old, kb, historical_web).run("微博2024年1月有哪些神乐皮肤消息")
    assert not historical_web.asked
    assert "历史命中按该区间核对，不因历史日期强制联网" in old.analyst_prompts[0]
