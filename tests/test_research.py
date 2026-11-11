"""查证工作流：分析问题、逐级取证、查证员判断、复核把关、打回、报进度。

模型、取证来源、别名查询、以图搜图都换成按剧本走的替身。
"""

import asyncio
from collections.abc import Collection
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from deployment import FULL_CONFIG_DIR
from jianshanagent.core.config import AppConfig, load_config
from jianshanagent.core.llm import LLMContentBlocked, LLMError
from jianshanagent.core.plugin import PluginContext
from jianshanagent.core.websearch import WebPage, WebSearchClient
from jianshanagent.plugins.knowledge.retrieval import RetrievalSettings, Retriever
from jianshanagent.plugins.knowledge.store import KbChunk, MemoryKnowledgeStore
from jianshanagent.plugins.research.analysis import Analysis, NameNote, Understanding
from jianshanagent.plugins.research.evidence import (
    EntryPages,
    Evidence,
    PageFiller,
    SiteSearchSource,
    WebSearchSource,
    render,
)
from jianshanagent.plugins.research.plugin import ResearchPlugin, wiki_name_lookup
from jianshanagent.plugins.research.workflow import (
    ANSWERED,
    FAILED,
    NOT_FOUND,
    Decision,
    Fixed,
    ResearchWorkflow,
    Verdict,
    WorkflowSettings,
)
from jianshanagent.plugins.wiki_query.client import NameMatch


class ScriptedLLM:
    """每个角色各有一份剧本；记下每次收到的提示词。条目是异常就抛出来。"""

    def __init__(
        self,
        decisions: list[Any],
        verdicts: list[Any] | None = None,
        *,
        analysis: Any = None,
        fixes: list[Any] | None = None,
    ):
        self.decisions = list(decisions)
        self.verdicts = list(verdicts or [])
        self.analysis = analysis
        self.fixes = list(fixes or [])
        self.analyst_prompts: list[str] = []
        self.review_prompts: list[str] = []
        self.analysis_prompts: list[str] = []
        self.fix_prompts: list[str] = []
        #: 每次调用的（要的结果，用的哪一档，最多试几次）
        self.calls: list[tuple[str, str, int]] = []

    async def chat_json(
        self, messages: Any, schema: Any, *, profile: str, attempts: int = 2
    ) -> Any:
        prompt = messages[0]["content"]
        self.calls.append((schema.__name__, profile, attempts))
        if schema is Decision:
            assert profile in ("reason", "quick")
            self.analyst_prompts.append(prompt)
            item = self.decisions.pop(0)
        elif schema is Analysis:
            assert profile == "quick"
            self.analysis_prompts.append(prompt)
            item = self.analysis
        elif schema is Fixed:
            assert profile == "quick"
            self.fix_prompts.append(prompt)
            item = self.fixes.pop(0)
        else:
            assert schema is Verdict and profile in ("review", "quick")
            self.review_prompts.append(prompt)
            item = self.verdicts.pop(0) if self.verdicts else Verdict(ok=True)
        if item is HANG:
            await asyncio.sleep(3600)
        if isinstance(item, Exception):
            raise item
        return item


#: 当成一次回复用：模型接口卡住不返回
HANG = TimeoutError("卡住")


class FakeSource:
    keywords_hint = "写关键词"

    def __init__(self, name: str, label: str, items: list[Evidence] | Exception):
        self.name, self.label, self.items = name, label, items
        self.asked: list[tuple[str, str]] = []

    async def collect(self, question: str, keywords: str, have: Collection[str]) -> list[Evidence]:
        self.asked.append((question, keywords))
        if isinstance(self.items, Exception):
            raise self.items
        return self.items


def ev(key: str, text: str, origin: str = "知识库") -> Evidence:
    return Evidence(
        origin=origin, title=f"标题{key}", url=f"https://w.test/{key}", text=text, key=key
    )


def workflow(
    llm: ScriptedLLM,
    *sources: Any,
    name_lookup: Any = None,
    entry_pages: Any = None,
    image_search: Any = None,
    page_filler: Any = None,
    **settings: Any,
) -> ResearchWorkflow:
    return ResearchWorkflow(
        llm,  # type: ignore[arg-type]
        sources,
        WorkflowSettings(**settings),
        name_lookup=name_lookup,
        entry_pages=entry_pages,
        image_search=image_search,
        page_filler=page_filler,
    )


async def test_answers_from_the_first_level_when_it_is_enough() -> None:
    kb = FakeSource("kb", "知识库", [ev("a", "水豚活动 3 日开"), ev("b", "无关")])
    site = FakeSource("site", "站内搜索", [])
    llm = ScriptedLLM(
        [Decision(action="answer", answer="3 日开", sources=[1])], [Verdict(ok=True, sources=[1])]
    )
    sent: list[str] = []

    async def on_progress(text: str) -> None:
        sent.append(text)

    flow = workflow(llm, kb, site, progress={"kb": "正在查知识库…", "review": "正在复核…"})
    outcome = await flow.run(" 水豚活动什么时候开 ", style="用纯文本", on_progress=on_progress)

    assert (outcome.status, outcome.answer, outcome.rejections) == (ANSWERED, "3 日开", 0)
    assert [item.key for item in outcome.cited] == ["a"]
    assert kb.asked == [("水豚活动什么时候开", "")] and site.asked == []  # 够了就不升级
    # 每到一个阶段报一次进度
    assert outcome.steps == ["kb", "review"] and sent == ["正在查知识库…", "正在复核…"]
    # 查证员看到了问题、带编号的资料、下一级是什么、对口吻的要求
    prompt = llm.analyst_prompts[0]
    assert "水豚活动什么时候开" in prompt and "【资料 1】知识库" in prompt and "3 日开" in prompt
    assert "「站内搜索」" in prompt and "写关键词" in prompt and "8. 用纯文本" in prompt
    # 复核员看到了问题、全部资料和结论
    review = llm.review_prompts[0]
    assert "【资料 2】" in review and review.rstrip().endswith("3 日开")


async def test_pages_from_the_web_are_cited_by_title_only() -> None:
    page = Evidence(
        origin="联网搜索",
        title="攻略（九游网）",
        url="",
        text="带丑女",
        kind="网页",
        key="https://n.test/1",
    )
    kb = FakeSource("kb", "知识库", [ev("a", "无关"), page])
    llm = ScriptedLLM([Decision(answer="网上搜到的：带丑女", sources=[2, 1, 2])])
    outcome = await workflow(llm, kb).run("永生之海怎么打")
    # 网页没有能发出去的链接，也照样列进参考资料（只有标题）；同一条只列一次
    assert [(item.title, item.url) for item in outcome.cited] == [
        ("攻略（九游网）", ""),
        ("标题a", "https://w.test/a"),
    ]


async def test_escalates_level_by_level_and_keeps_all_evidence() -> None:
    kb = FakeSource("kb", "知识库", [ev("a", "无关")])
    site = FakeSource("site", "站内搜索", [ev("a", "无关"), ev("s", "奖励是头像框", "站内搜索")])
    web = FakeSource("web", "联网搜索", [ev("w", "网上说的", "联网搜索")])
    llm = ScriptedLLM(
        [
            Decision(action="search", keywords="水豚 奖励"),
            Decision(action="search", keywords="阴阳师 水豚 奖励"),
            Decision(action="answer", answer="奖励是头像框", sources=[2, 9]),
        ]
    )
    outcome = await workflow(llm, kb, site, web).run("水豚活动的奖励")

    assert outcome.status == ANSWERED
    assert site.asked == [("水豚活动的奖励", "水豚 奖励")]
    assert web.asked == [("水豚活动的奖励", "阴阳师 水豚 奖励")]
    # 资料逐级累加，重复的不再收；复核没给引用时用查证员的，越界的编号不要
    assert [item.key for item in outcome.evidence] == ["a", "s", "w"]
    assert [item.key for item in outcome.cited] == ["s"]
    assert outcome.steps == ["kb", "site", "web", "review"]
    # 最后一级也查过了：告诉查证员没有别的地方可查
    assert (
        "已经没有别的地方可查" in llm.analyst_prompts[2] and "search" not in llm.analyst_prompts[2]
    )
    assert "【资料 3】联网搜索" in llm.analyst_prompts[2]


async def test_sources_in_one_level_are_collected_together_before_the_analyst_looks() -> None:
    kb = FakeSource("kb", "知识库", [ev("a", "无关")])
    site = FakeSource("site", "站内搜索", [ev("a", "无关"), ev("s", "奖励是头像框", "站内搜索")])
    analysis = Analysis(names=["水豚活动"], keywords=["水豚活动", "奖励"])
    sent: list[str] = []

    async def on_progress(text: str) -> None:
        sent.append(text)

    # 第一级：知识库和站内搜索一起查，没有再下一级
    llm = ScriptedLLM([Decision(answer="奖励是头像框", sources=[2])], analysis=analysis)
    flow = workflow(
        llm, [kb, site], analysis_profile="quick", progress={"kb": "查资料", "site": "补搜"}
    )
    outcome = await flow.run("水豚活动的奖励", on_progress=on_progress)
    assert outcome.status == ANSWERED and [item.key for item in outcome.evidence] == ["a", "s"]
    # 站内搜索用的是分析问题时挑的词；查证员第一眼就看到两处的资料，而且没有别处可查了
    assert site.asked == [("水豚活动的奖励", "水豚活动 奖励")]
    assert "【资料 2】站内搜索" in llm.analyst_prompts[0]
    assert "已经没有别的地方可查" in llm.analyst_prompts[0] and len(llm.analyst_prompts) == 1
    # 一级只报一句进度（第一个来源的），走过的来源都记着
    assert sent == ["查资料"]
    assert outcome.steps == ["analyze", "kb", "site", "review"] and outcome.levels == 1

    # 后面还有一级（还是站内搜索）：告诉查证员前面用什么词搜过，让它换词
    kb.asked.clear(), site.asked.clear(), sent.clear()
    llm = ScriptedLLM(
        [Decision(action="search", keywords="水豚 头像框"), Decision(answer="奖励是头像框")],
        analysis=analysis,
    )
    flow = workflow(
        llm, [kb, site], site, analysis_profile="quick", progress={"kb": "查资料", "site": "补搜"}
    )
    outcome = await flow.run("水豚活动的奖励", on_progress=on_progress)
    assert "「站内搜索」已经用「水豚活动 奖励」搜过，这次要换不一样的词" in llm.analyst_prompts[0]
    assert [keywords for _, keywords in site.asked] == ["水豚活动 奖励", "水豚 头像框"]
    assert sent == ["查资料", "补搜"]
    assert outcome.steps == ["analyze", "kb", "site", "site", "review"] and outcome.levels == 2

    # 一级里有一个来源出错：不影响同一级的别的来源
    broken = FakeSource("kb", "知识库", RuntimeError("数据库断了"))
    llm = ScriptedLLM([Decision(answer="奖励是头像框", sources=[2])], analysis=analysis)
    outcome = await workflow(llm, [broken, site], analysis_profile="quick").run("水豚活动的奖励")
    assert [item.key for item in outcome.evidence] == ["a", "s"]

    with pytest.raises(ValueError):
        workflow(llm, [])


async def test_empty_or_broken_level_is_skipped_without_asking_the_model_again() -> None:
    kb = FakeSource("kb", "知识库", [])
    site = FakeSource("site", "站内搜索", RuntimeError("wiki 挂了"))
    web = FakeSource("web", "联网搜索", [ev("w", "网上说 3 日开", "联网搜索")])
    llm = ScriptedLLM(
        [Decision(action="search", keywords="水豚"), Decision(action="answer", answer="3 日开")]
    )
    outcome = await workflow(llm, kb, site, web).run("水豚")
    assert outcome.status == ANSWERED and len(llm.analyst_prompts) == 2
    assert web.asked == [("水豚", "水豚")]  # 站内搜索没查到，带着同一个搜索词直接联网
    assert outcome.steps == ["kb", "site", "web", "review"]
    assert "（什么都没查到）" in llm.analyst_prompts[0]


async def test_rejected_conclusion_goes_back_to_the_analyst() -> None:
    kb = FakeSource("kb", "知识库", [ev("a", "水豚活动 3 日开")])
    site = FakeSource("site", "站内搜索", [ev("s", "奖励是头像框", "站内搜索")])
    llm = ScriptedLLM(
        [
            Decision(action="answer", answer="5 日开"),
            Decision(action="search", keywords="水豚"),  # 被打回后可以选择接着查
            Decision(action="answer", answer="3 日开", sources=[1]),
        ],
        [Verdict(ok=False, problems=["日期和资料 1 不符"]), Verdict(ok=True, sources=[1, 2])],
    )
    sent: list[str] = []

    async def on_progress(text: str) -> None:
        sent.append(text)
        if len(sent) == 1:
            raise RuntimeError("发不出去")

    texts = {"kb": "查知识库", "site": "", "revise": "修正中"}
    flow = workflow(llm, kb, site, progress=texts)
    outcome = await flow.run("水豚活动什么时候开", on_progress=on_progress)

    assert (outcome.status, outcome.answer, outcome.rejections) == (ANSWERED, "3 日开", 1)
    assert outcome.problems == [] and [item.key for item in outcome.cited] == ["a", "s"]
    assert outcome.steps == ["kb", "review", "revise", "site", "review"]
    # 没配文字的阶段（复核、站内搜索）不报；进度报不出去不影响查证
    assert sent == ["查知识库", "修正中"]
    # 打回时把上一版回答和复核意见都带给查证员
    assert "5 日开" in llm.analyst_prompts[1] and "日期和资料 1 不符" in llm.analyst_prompts[1]
    assert "打回" not in llm.analyst_prompts[0]


async def test_gives_up_honestly_after_too_many_rejections_or_model_failure() -> None:
    kb = FakeSource("kb", "知识库", [ev("a", "水豚活动 3 日开")])
    bad = Verdict(ok=False, problems=["没有依据"])
    llm = ScriptedLLM([Decision(answer="5 日开"), Decision(answer="6 日开")], [bad, bad])
    outcome = await workflow(llm, kb, max_rejections=1).run("水豚")
    assert (outcome.status, outcome.answer, outcome.rejections) == (NOT_FOUND, "", 2)
    assert outcome.problems == ["没有依据"]

    # 被打回时已经花了太久：不再重写
    llm = ScriptedLLM([Decision(answer="5 日开"), Decision(answer="不该走到这")], [bad])
    outcome = await workflow(llm, kb, max_seconds=0).run("水豚")
    assert (outcome.status, outcome.rejections, len(llm.decisions)) == (NOT_FOUND, 1, 1)

    # 查证员说要搜索但已经没有下一级、又没写回答：没查到
    outcome = await workflow(ScriptedLLM([Decision(action="search", keywords="x")]), kb).run("水豚")
    assert outcome.status == NOT_FOUND

    # 查证员的接口出错：失败；复核的接口出错：不拦结论
    outcome = await workflow(ScriptedLLM([LLMError("挂了")]), kb).run("水豚")
    assert outcome.status == FAILED
    llm = ScriptedLLM([Decision(answer="3 日开", sources=[1])], [LLMError("挂了")])
    outcome = await workflow(llm, kb).run("水豚")
    assert outcome.status == ANSWERED and [item.key for item in outcome.cited] == ["a"]

    # 内容被审核拦截要让上层知道
    with pytest.raises(LLMContentBlocked):
        await workflow(ScriptedLLM([LLMContentBlocked("拦截", status=421)]), kb).run("水豚")
    # 复核那一次被拦也一样：问题和资料查证员那次没被拦，多出来的只有那份回答，不能照发
    llm = ScriptedLLM([Decision(answer="3 日开")], [LLMContentBlocked("拦截", status=421)])
    with pytest.raises(LLMContentBlocked):
        await workflow(llm, kb).run("水豚")
    # 复核过没过必须明说：只列了问题、漏写 ok 的输出不合格（会带着错误让模型重写），不能当成通过
    with pytest.raises(ValidationError):
        Verdict.model_validate({"problems": ["日期在资料里找不到"]})


async def test_out_of_rounds_or_time_fixes_the_flagged_parts_instead_of_giving_up() -> None:
    kb = FakeSource("kb", "知识库", [ev("a", "水豚活动 3 日开，奖励头像框")])
    bad = Verdict(ok=False, problems=["「还送皮肤」没有依据，应删掉"], sources=[1])
    draft = Decision(answer="3 日开，奖励头像框，还送皮肤", sources=[1])
    sent: list[str] = []

    async def on_progress(text: str) -> None:
        sent.append(text)

    # 被打回时已经花了太久：不让查证员重写，按复核意见改掉被点名的地方就发
    llm = ScriptedLLM([draft], [bad], fixes=[Fixed(answer=" 3 日开，奖励头像框 ")])
    flow = workflow(llm, kb, max_seconds=0, quick_profile="quick", progress={"fix": "修正中"})
    outcome = await flow.run("水豚活动什么时候开", on_progress=on_progress)
    assert (outcome.status, outcome.answer, outcome.fixed) == (ANSWERED, "3 日开，奖励头像框", True)
    assert outcome.steps == ["kb", "review", "fix"] and sent == ["修正中"]
    assert [item.key for item in outcome.cited] == ["a"] and outcome.rejections == 1
    # 修正的人看到了原回答、复核意见和资料
    prompt = llm.fix_prompts[0]
    assert "还送皮肤" in prompt and "应删掉" in prompt and "【资料 1】" in prompt

    # 打回次数用完也一样；改完什么都不剩、或者修正的接口出错：才说没查到
    for fix in (Fixed(answer=""), LLMError("挂了")):
        llm = ScriptedLLM([draft, draft], [bad, bad], fixes=[fix])
        outcome = await workflow(llm, kb, max_rejections=1, quick_profile="quick").run("水豚")
        assert (outcome.status, outcome.rejections, outcome.fixed) == (NOT_FOUND, 2, False)
        assert outcome.steps == ["kb", "review", "revise", "review", "fix"]


async def test_thinking_calls_that_fail_are_redone_once_without_thinking() -> None:
    kb = FakeSource("kb", "知识库", [ev("a", "水豚活动 3 日开")])
    # 查证员、复核员的那次带思考的调用都没成（思考把额度用光、超时）
    llm = ScriptedLLM(
        [LLMError("输出被截断"), Decision(answer="3 日开", sources=[1])],
        [LLMError("超时"), Verdict(ok=True, sources=[1])],
    )
    outcome = await workflow(llm, kb, quick_profile="quick").run("水豚活动什么时候开")
    assert (outcome.status, outcome.answer) == (ANSWERED, "3 日开")
    # 带思考的只试一次（重来一遍还是一两分钟），然后换不开思考的那一档
    assert llm.calls == [
        ("Decision", "reason", 1),
        ("Decision", "quick", 2),
        ("Verdict", "review", 1),
        ("Verdict", "quick", 2),
    ]
    assert llm.analyst_prompts[0] == llm.analyst_prompts[1]  # 问的是同一份提示词

    # 两档都不成：查证员算失败
    llm = ScriptedLLM([LLMError("a"), LLMError("b")])
    assert (await workflow(llm, kb, quick_profile="quick").run("水豚")).status == FAILED


async def test_stalled_calls_are_cut_off_and_nothing_runs_past_the_deadline() -> None:
    kb = FakeSource("kb", "知识库", [ev("a", "水豚活动 3 日开")])
    # 带思考的调用卡住不返回：等到限时就不等了，改用不开思考的
    llm = ScriptedLLM([HANG, Decision(answer="3 日开", sources=[1])], [HANG, Verdict(ok=True)])
    flow = workflow(llm, kb, quick_profile="quick", think_timeout_seconds=0.01)
    outcome = await flow.run("水豚活动什么时候开")
    assert (outcome.status, outcome.answer) == (ANSWERED, "3 日开")
    assert [call[:2] for call in llm.calls] == [
        ("Decision", "reason"),
        ("Decision", "quick"),
        ("Verdict", "review"),
        ("Verdict", "quick"),
    ]

    # 离最后期限已经不够再想一次：不开带思考的调用，直接用不开思考的；
    # 它也卡住的话等到最后期限为止——复核没做成不拦结论
    llm = ScriptedLLM([Decision(answer="3 日开", sources=[1])], [HANG])
    outcome = await workflow(llm, kb, quick_profile="quick", deadline_seconds=0.05).run("水豚")
    assert outcome.status == ANSWERED and outcome.seconds < 1
    assert [call[:2] for call in llm.calls] == [("Decision", "quick"), ("Verdict", "quick")]

    # 没配不开思考的那一档：带思考的调用卡住就算失败
    llm = ScriptedLLM([HANG])
    assert (await workflow(llm, kb, think_timeout_seconds=0.01).run("水豚")).status == FAILED


async def test_roles_given_a_profile_without_thinking_only_wait_the_short_limit() -> None:
    kb = FakeSource("kb", "知识库", [ev("a", "水豚活动 3 日开")])
    # 查证员、复核员配成了不开思考的档位（比如便宜的那个模型）：
    # 卡住时只等短的那个限时，然后换兜底的那一档
    llm = ScriptedLLM([HANG, Decision(answer="3 日开", sources=[1])], [HANG, Verdict(ok=True)])
    flow = workflow(
        llm,
        kb,
        analyst_profile="reason",
        reviewer_profile="review",
        quick_profile="quick",
        fast_profiles=frozenset({"reason", "review", "quick"}),
        quick_timeout_seconds=0.01,
    )  # think_timeout_seconds 还是默认的 75 秒：要是按它等，这个测试会卡住
    outcome = await asyncio.wait_for(flow.run("水豚活动什么时候开"), 5)
    assert (outcome.status, outcome.answer) == (ANSWERED, "3 日开")
    assert [call[:2] for call in llm.calls] == [
        ("Decision", "reason"),
        ("Decision", "quick"),
        ("Verdict", "review"),
        ("Verdict", "quick"),
    ]

    # 配的就是兜底的那一档：直接问它，不先试一次再兜底
    llm = ScriptedLLM([Decision(answer="3 日开", sources=[1])], [Verdict(ok=True)])
    flow = workflow(
        llm, kb, analyst_profile="quick", reviewer_profile="quick", quick_profile="quick"
    )
    assert (await flow.run("水豚活动什么时候开")).status == ANSWERED
    assert llm.calls == [("Decision", "quick", 2), ("Verdict", "quick", 2)]


async def test_an_analyst_on_the_fallback_profile_is_asked_again_when_the_call_stalls() -> None:
    kb = FakeSource("kb", "知识库", [ev("a", "水豚活动 3 日开")])
    quick = {
        "analyst_profile": "quick",
        "reviewer_profile": "quick",
        "quick_profile": "quick",
        "quick_timeout_seconds": 0.01,
    }
    # 查证员配的就是兜底的那一档：没有别的档位可换，卡住了原样再问一次
    # （这一步做不成整个查证就失败，而卡住常常只是那一个请求的事）
    llm = ScriptedLLM([HANG, Decision(answer="3 日开", sources=[1])], [Verdict(ok=True)])
    outcome = await asyncio.wait_for(workflow(llm, kb, **quick).run("水豚活动什么时候开"), 5)
    assert (outcome.status, outcome.answer) == (ANSWERED, "3 日开")
    assert [call[0] for call in llm.calls] == ["Decision", "Decision", "Verdict"]
    assert llm.analyst_prompts[0] == llm.analyst_prompts[1]  # 问的是同一份提示词

    # 再问还是卡住：失败。只多问一次，不会一直问下去
    llm = ScriptedLLM([HANG, HANG, Decision(answer="不该走到这")])
    outcome = await asyncio.wait_for(workflow(llm, kb, **quick).run("水豚"), 5)
    assert outcome.status == FAILED and len(llm.decisions) == 1
    # 配成不重试的话，卡住一次就失败
    llm = ScriptedLLM([HANG, Decision(answer="不该走到这")])
    outcome = await asyncio.wait_for(workflow(llm, kb, **quick, quick_retries=0).run("水豚"), 5)
    assert outcome.status == FAILED and len(llm.decisions) == 1

    # 接口报错不在这里再问（模型调用层自己重试过了）；复核卡住也不再问——复核做不成不拦结论
    llm = ScriptedLLM([LLMError("挂了"), Decision(answer="不该走到这")])
    assert (await workflow(llm, kb, **quick).run("水豚")).status == FAILED
    assert len(llm.decisions) == 1
    llm = ScriptedLLM([Decision(answer="3 日开", sources=[1])], [HANG, Verdict(ok=False)])
    outcome = await asyncio.wait_for(workflow(llm, kb, **quick).run("水豚"), 5)
    assert outcome.status == ANSWERED and len(llm.verdicts) == 1

    # 最后期限到了就不再问
    llm = ScriptedLLM([HANG, Decision(answer="不该走到这")])
    flow = workflow(llm, kb, **{**quick, "quick_timeout_seconds": 30}, deadline_seconds=0.05)
    outcome = await asyncio.wait_for(flow.run("水豚"), 5)
    assert outcome.status == FAILED and len(llm.decisions) == 1


async def test_a_rewrite_that_fails_falls_back_to_fixing_the_draft_that_was_rejected() -> None:
    kb = FakeSource("kb", "知识库", [ev("a", "水豚活动 3 日开，奖励头像框")])
    bad = Verdict(ok=False, problems=["「还送皮肤」没有依据，应删掉"], sources=[1])
    draft = Decision(answer="3 日开，奖励头像框，还送皮肤", sources=[1])
    quick = {
        "analyst_profile": "quick",
        "reviewer_profile": "quick",
        "quick_profile": "quick",
        "quick_timeout_seconds": 0.01,
    }
    # 被打回后重写的那次调用没做成（接口出错，或者卡住）：手里那一版结论还在，
    # 按复核意见改掉被点名的地方就发，而不是让用户等了半天只得到一句「有点忙」
    for failure in (LLMError("挂了"), HANG):
        llm = ScriptedLLM([draft, failure], [bad], fixes=[Fixed(answer="3 日开，奖励头像框")])
        outcome = await asyncio.wait_for(workflow(llm, kb, **quick).run("水豚活动"), 5)
        assert (outcome.status, outcome.answer) == (ANSWERED, "3 日开，奖励头像框")
        assert (outcome.fixed, outcome.rejections) == (True, 1)
        assert outcome.steps == ["kb", "review", "revise", "fix"]
        assert [item.key for item in outcome.cited] == ["a"]  # 引用的是被打回那一版用的资料
        # 重写卡住时不再问第二次：已经有退路了
        assert [call[0] for call in llm.calls] == ["Decision", "Verdict", "Decision", "Fixed"]
        assert "还送皮肤" in llm.fix_prompts[0] and "应删掉" in llm.fix_prompts[0]

    # 修正也没做成、或者没配修正用的那一档：才算失败
    llm = ScriptedLLM([draft, LLMError("挂了")], [bad], fixes=[LLMError("也挂了")])
    outcome = await workflow(llm, kb, **quick).run("水豚活动")
    assert (outcome.status, outcome.answer, outcome.fixed) == (FAILED, "", False)
    llm = ScriptedLLM([draft, LLMError("a"), LLMError("b")], [bad])
    assert (await workflow(llm, kb).run("水豚活动")).status == FAILED
    assert llm.fix_prompts == []


# ---------- 第 0 步：弄明白问题 ----------


async def test_analysis_resolves_aliases_and_time_for_retrieval_and_for_the_models() -> None:
    kb = FakeSource("kb", "知识库", [ev("a", "白童子的羁绊是冥府、甲胄")])
    analysis = Analysis(
        topic="玩法",
        names=["小小白", "百鬼棋局", "白童子", "大蛇"],
        time="2026年",
        keywords=["小小白", "羁绊", "白童子的技能"],
    )
    llm = ScriptedLLM([Decision(answer="冥府、甲胄", sources=[1])], analysis=analysis)
    looked: list[str] = []

    async def lookup(name: str) -> list[NameNote]:
        looked.append(name)
        if name == "小小白":
            return [NameNote(name, "白童子", "式神", ("小小白",))]
        if name == "大蛇":
            return [
                NameNote(name, "八岐大蛇", "式神", exact=False),
                NameNote(name, "神堕八岐大蛇", "式神", exact=False),
            ]
        if name == "百鬼棋局":
            return [NameNote(name, "百鬼棋局", "页面")]
        raise RuntimeError("wiki 挂了")

    class Entries:
        asked: list[tuple[str, list[str]]] = []

        async def collect(self, query: str, titles: Any, have: Any) -> list[Evidence]:
            self.asked.append((query, list(titles)))
            return [ev("p", "白童子：2 费棋子")]

    entries = Entries()
    flow = workflow(
        llm,
        kb,
        name_lookup=lookup,
        entry_pages=entries,
        analysis_profile="quick",
        analysis_topics=("式神", "玩法", "其它"),  # 问题分哪几类因站而异
    )
    question = "今年小小白在百鬼棋局里和大蛇有羁绊吗"
    outcome = await flow.run(question)

    assert outcome.status == ANSWERED and outcome.steps == ["analyze", "kb", "review"]
    # 模型摘出来、但原话里没有的名字（白童子）不查；查别名出错不影响别的
    assert looked == ["小小白", "百鬼棋局", "大蛇"]
    # 点到名、wiki 上确实有的条目（候选不算），先取它们自己页面里的内容，排在资料最前面
    assert entries.asked == [(outcome.query, ["白童子", "百鬼棋局"])]
    assert [item.key for item in outcome.evidence] == ["p", "a"]
    # 检索用的问句补上了正式名和具体时间；候选（大蛇）不往里补
    # 站内搜索用的词：模型挑的词里只留原话里真有的（「白童子的技能」不是），外号换成正式名
    assert (
        kb.asked == [(f"{question}（小小白即白童子；2026年）", "白童子 羁绊")]
        and outcome.query == kb.asked[0][0]
    )
    # 分析的提示词里有现在的时间和用户的原话
    assert "现在是 20" in llm.analysis_prompts[0] and question in llm.analysis_prompts[0]
    # 查证员和复核员都看到了原话、对照关系和现在的时间
    for prompt in (llm.analyst_prompts[0], llm.review_prompts[0]):
        assert f"用户的问题：\n{question}\n" in prompt and "现在是 20" in prompt
        assert "- 这个问题问的是「玩法」一类。" in prompt
        assert "- 「小小白」是式神「白童子」的别名。" in prompt
        assert "名字里带它的有：式神「八岐大蛇」、式神「神堕八岐大蛇」。" in prompt
        assert "- 问题里说的时间指 2026年。" in prompt
        assert "百鬼棋局」的别名" not in prompt  # 写的就是正式名，没什么可说的


async def test_analysis_failure_falls_back_to_the_question_as_asked() -> None:
    kb = FakeSource("kb", "知识库", [ev("a", "3 日开")])
    llm = ScriptedLLM([Decision(answer="3 日开")], analysis=LLMError("挂了"))
    outcome = await workflow(llm, kb, analysis_profile="quick").run("水豚活动什么时候开")
    assert outcome.status == ANSWERED and kb.asked == [("水豚活动什么时候开", "")]
    assert "程序预先查到的" not in llm.analyst_prompts[0]

    # 模型一个能用的搜索词都没挑出来：用查到的正式名
    async def lookup(name: str) -> list[NameNote]:
        return [NameNote(name, "白童子", "式神", ("小小白",))]

    llm = ScriptedLLM(
        [Decision(answer="2 费")], analysis=Analysis(names=["小小白"], keywords=["几费", "白童子"])
    )
    flow = workflow(llm, kb, name_lookup=lookup, analysis_profile="quick")
    await flow.run("小小白是多少费的")
    assert kb.asked[-1][1] == "白童子"

    # 类别不在给定的范围里就不要；问题里本来就写了的时间不重复补
    understanding = Understanding(time="2024年")
    assert understanding.search_query("2024年的周年庆") == "2024年的周年庆"


async def test_entry_pages_take_the_closest_chunks_of_each_named_page() -> None:
    def page(chunk_id: str, title: str, body: str) -> tuple[KbChunk, None]:
        return KbChunk(chunk_id, f"page:{chunk_id}", "entry", title, f"{title}\n\n{body}"), None

    store = MemoryKnowledgeStore()
    await store.sync(
        [
            page("w1", "白童子", "白童子的技能是招魂"),
            page("w2", "白童子", "白童子的传记"),
            page("w3", "白童子", "白童子的技能升级"),
            page("b1", "百鬼棋局", "百鬼棋局的技能说明"),
            page("x", "别的页面", "技能"),
        ],
        fingerprint="t",
    )
    retriever = Retriever(store, None, RetrievalSettings(max_df_ratio=1.0))
    pages = EntryPages(retriever, label="知识库", per_name=2, total=3)
    found = await pages.collect(
        "白童子的技能", ["白童子", "百鬼棋局", "白童子", "没有的页面"], {"w1"}
    )
    # 每个条目最多 2 片、合计最多 3 片；已经有的（w1）不再给；别的页面的不要
    assert [(item.key, item.origin) for item in found] == [
        ("w3", "知识库"),
        ("w2", "知识库"),
        ("b1", "知识库"),
    ]


async def test_wiki_name_lookup_translates_the_wiki_clients_matches() -> None:
    async def lookup_name(name: str) -> list[NameMatch]:
        return [NameMatch("白童子", "character", ("小小白",)), NameMatch("白童子", "soul")]

    # 条目的种类给模型看的说法是站点插件定的
    labels = {"character": "式神", "soul": "御魂"}
    wiki = SimpleNamespace(
        lookup_name=lookup_name, kind_label=lambda kind: labels.get(kind, "页面")
    )
    notes = await wiki_name_lookup(wiki)("小小白")
    assert notes == [
        NameNote("小小白", "白童子", "式神", ("小小白",)),
        NameNote("小小白", "白童子", "御魂"),
    ]


# ---------- 用户发了图片 ----------


class FakeImageSearch:
    def __init__(self, results: list[Any]):
        self.results = list(results)
        self.asked: list[bytes] = []

    async def search(self, image: bytes, *, top_k: int | None = None) -> list[Any]:
        self.asked.append(image)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def hit(image_id: str, label: str, score: float, sure: bool) -> Any:
    return SimpleNamespace(
        image_id=image_id,
        label=label,
        kind="皮肤立绘",
        page_title="不知火",
        url=f"https://w.test/{image_id}",
        score=score,
        sure=sure,
        text="获取方式：皮肤商店",
    )


async def test_images_are_searched_first_and_what_they_show_feeds_retrieval() -> None:
    kb = FakeSource("kb", "知识库", [ev("a", "离岛之歌在皮肤商店出售")])
    images = FakeImageSearch(
        [[hit("s1", "不知火的皮肤「离岛之歌」", 0.93, True), hit("s2", "别的皮肤", 0.6, False)]]
    )
    llm = ScriptedLLM([Decision(answer="这是不知火的皮肤「离岛之歌」", sources=[1])])
    sent: list[str] = []

    async def on_progress(text: str) -> None:
        sent.append(text)

    class Entries:
        asked: list[list[str]] = []

        async def collect(self, query: str, titles: Any, have: Any) -> list[Evidence]:
            self.asked.append(list(titles))
            return [ev("p", "不知火的皮肤一览")]

    entries = Entries()
    flow = workflow(
        llm,
        kb,
        image_search=images,
        entry_pages=entries,
        max_images=1,
        progress={"image": "搜图中"},
    )
    outcome = await flow.run("这是哪个皮肤", on_progress=on_progress, images=[b"one", b"two"])

    assert outcome.status == ANSWERED and outcome.steps == ["image", "kb", "review"]
    assert images.asked == [b"one"] and sent == ["搜图中"]  # 最多只拿 max_images 张去搜
    # 认出来的图：它是什么补进检索问句，它所在的条目先取自己页面里的内容；图片资料排在最前面
    assert kb.asked == [("这是哪个皮肤（图片是：不知火的皮肤「离岛之歌」）", "")]
    assert entries.asked == [["不知火"]]
    assert [item.key for item in outcome.evidence] == ["image:s1", "image:s2", "p", "a"]
    assert [item.url for item in outcome.cited] == ["https://w.test/s1"]
    prompt = llm.analyst_prompts[0]
    assert "【资料 1】以图搜图｜皮肤立绘" in prompt
    assert "认定，用户发的图片就是 wiki 上的这张图（相似度 0.93" in prompt
    assert "获取方式：皮肤商店" in prompt
    assert "和 wiki 上的这张图有些像（相似度 0.60），但不能确定是同一张" in prompt
    assert "用户发了 1 张图片，在 wiki 图库里找到的相似图片列在资料里" in prompt
    assert "用户发了 1 张图片" in llm.review_prompts[0]


async def test_images_that_match_nothing_are_reported_as_unrecognised() -> None:
    kb = FakeSource("kb", "知识库", [ev("a", "无关")])
    # 没搜到、搜图出错、压根没有图片索引：都告诉模型没认出来，不让它猜
    for image_search in (FakeImageSearch([[]]), FakeImageSearch([RuntimeError("坏图")]), None):
        llm = ScriptedLLM([Decision(answer="没认出这张图")])
        outcome = await workflow(llm, kb, image_search=image_search).run("这是谁", images=[b"x"])
        assert outcome.status == ANSWERED and kb.asked[-1] == ("这是谁", "")
        assert "没有找到对得上的图" in llm.analyst_prompts[0]
        assert "不要猜图里是谁" in llm.analyst_prompts[0]


async def test_evidence_budget_and_rendering() -> None:
    kb = FakeSource("kb", "知识库", [ev("a", "甲" * 60), ev("b", "乙" * 60), ev("c", "丙" * 10)])
    outcome = await workflow(ScriptedLLM([Decision(answer="答")]), kb, evidence_chars=80).run("问")
    assert [item.key for item in outcome.evidence] == ["a", "c"]  # 放不下的不要，后面放得下的照收

    dated = Evidence(
        origin="知识库",
        title="公告",
        url="u",
        text="公告 ｜ 发布于 2024\n正文",
        kind="公告",
        date="2024-08-07",
    )
    assert render([dated, ev("x", "正文")]) == (
        "【资料 1】知识库｜公告｜日期 2024-08-07\n链接：u\n公告 ｜ 发布于 2024\n正文\n\n"
        "【资料 2】知识库\n标题：标题x\n链接：https://w.test/x\n正文"
    )


async def test_site_search_takes_chunks_from_the_pages_it_finds() -> None:
    def page(chunk_id: str, title: str, body: str) -> tuple[KbChunk, None]:
        chunk = KbChunk(
            chunk_id=chunk_id,
            doc_key=f"page:{chunk_id}",
            kind="entry",
            title=title,
            text=f"{title}\n\n{body}",
            url=f"https://w.test/{chunk_id}",
        )
        return chunk, None

    store = MemoryKnowledgeStore()
    await store.sync(
        [
            page("d", "协同斗技", "协同斗技每周五开放，有奖励"),
            page("e1", "协同斗技", "协同斗技的奖励是勾玉"),
            page("a", "大天狗", "大天狗的奖励"),
        ],
        fingerprint="t",
    )
    asked: list[tuple[str, int]] = []

    async def search(keywords: str, limit: int) -> list[str]:
        asked.append((keywords, limit))
        return ["协同斗技", "库里没有的页面"]

    retriever = Retriever(store, None, RetrievalSettings(max_df_ratio=1.0))
    source = SiteSearchSource(retriever, search, pages=3, chunks=5)
    found = await source.collect("协同斗技的奖励", " 协同斗技 奖励 ", {"d"})
    assert asked == [("协同斗技 奖励", 3)]
    # 只要命中页面里的切片；已经有的（d）不再给
    assert [(item.key, item.origin, item.kind) for item in found] == [("e1", "站内搜索", "条目")]
    assert await source.collect("协同斗技的奖励", " ", set()) == []  # 没有搜索词就不搜


async def _boss_pages() -> Retriever:
    """「超鬼王」页切成 4 片（6 星占 3 片）；「式神」页很长；「公告」页只有一片。"""

    def piece(chunk_id: str, doc: str, seq: int, body: str, *path: str) -> KbChunk:
        return KbChunk(chunk_id, doc, "entry", doc, f"{doc}\n\n{body}", section_path=path, seq=seq)

    store = MemoryKnowledgeStore()
    chunks = [
        piece("b0", "超鬼王", 0, "2026 年的数据"),
        piece("b1", "超鬼王", 1, "蟹姬、蚀月吸血姬", "6星"),
        piece("b2", "超鬼王", 2, "毗沙门天、祸津神", "6星"),
        piece("b3", "超鬼王", 3, "雪御前", "6星", "雪御前"),
        piece("s0", "式神", 0, "技能" * 3000, "技能"),
        piece("s1", "式神", 1, "传记" * 3000, "传记"),
        piece("s2", "式神", 2, "传记二" * 2000, "传记"),
        piece("s3", "式神", 3, "传记三" * 2000, "传记"),
        piece("n0", "公告", 0, "活动 11 日开"),
    ]
    await store.sync([(chunk, None) for chunk in chunks], fingerprint="t:m:8")
    return Retriever(store, None)


def _picked(key: str, origin: str = "知识库") -> Evidence:
    return Evidence(origin=origin, title=key, url="", text=f"（{key} 的正文）", key=key)


async def test_page_filler_completes_picked_pages_and_marks_what_stays_partial() -> None:
    filler = PageFiller(await _boss_pages(), min_hits=2, max_chars=8000)
    web = Evidence(origin="联网搜索", title="网页", url="", text="网上的", key="https://n.test/1")
    found = [
        _picked("s1"),
        _picked("b2", "站内搜索"),
        web,
        _picked("n0"),
        _picked("b1"),
        _picked("s2"),
    ]
    filled = await filler.fill(found, 0, 5000)
    # 「超鬼王」挑中两片 → 补成整页，排在一起、按页面里的先后，位置在最先查到的那一片那里
    assert [item.key for item in filled] == ["s1", "b0", "b1", "b2", "b3", web.key, "n0", "s2"]
    by_key = {item.key: item for item in filled}
    assert "雪御前" in by_key["b3"].text and by_key["b3"].origin == "站内搜索"
    # 「式神」页太长，补不了：挑中的两片标成节选；整页的、只有一片的、不是知识库的都不标
    assert {item.key for item in filled if item.excerpt} == {"s1", "s2"}
    text = render(filled)
    assert "【资料 1】知识库｜节选" in text and "【资料 2】站内搜索｜条目\n" in text

    # 放不下就整页不补，挑中的照旧、标成节选
    tight = await filler.fill(found, 0, 3)
    assert [item.key for item in tight] == [item.key for item in found]
    assert {item.key for item in tight if item.excerpt} == {"s1", "s2", "b1", "b2"}

    # 前几级的资料位置不动；这一级又挑中同一页的一片，凑够了两片，补进来的排在这一级里
    later = await filler.fill([_picked("b1"), _picked("n0"), _picked("b3")], 2, 5000)
    assert [item.key for item in later] == ["b1", "n0", "b0", "b2", "b3"]
    assert not any(item.excerpt for item in later)


async def test_workflow_fills_pages_after_each_level_and_tells_the_analyst_about_excerpts() -> None:
    kb = FakeSource("kb", "知识库", [_picked("b1"), _picked("s1"), _picked("b2")])
    site = FakeSource("site", "站内搜索", [_picked("n0", "站内搜索")])
    llm = ScriptedLLM(
        [
            Decision(action="search", keywords="超鬼王"),
            Decision(answer="蟹姬、蚀月吸血姬、毗沙门天、祸津神、雪御前", sources=[4, 2]),
        ]
    )
    flow = workflow(llm, kb, site, page_filler=PageFiller(await _boss_pages()), evidence_chars=200)
    outcome = await flow.run("六星鬼王分别有哪些")
    # 第一级：超鬼王补成整页（挑中的在前、放得下），式神页那一片是节选
    assert [item.key for item in outcome.evidence] == ["b0", "b1", "b2", "b3", "s1", "n0"]
    first = llm.analyst_prompts[0]
    assert "雪御前" in first and "【资料 5】知识库｜节选" in first and "【资料 6】" not in first
    assert "标着「节选」的资料只是所在页面的一部分" in first
    assert [item.key for item in outcome.cited] == ["b3", "b1"]

    class Broken:
        async def fill(self, evidence: Any, start: int, room: int) -> list[Evidence]:
            raise RuntimeError("库连不上")

    # 补齐这一步出错不影响查证
    plain = workflow(ScriptedLLM([Decision(answer="蟹姬")]), kb, page_filler=Broken())
    outcome = await plain.run("六星鬼王分别有哪些")
    assert outcome.status == ANSWERED and [e.key for e in outcome.evidence] == ["b1", "s1", "b2"]


async def test_web_search_turns_pages_into_evidence_without_links() -> None:
    asked: list[str] = []

    async def search(query: str) -> list[WebPage]:
        asked.append(query)
        return [
            WebPage(
                title="永生之海攻略",
                url="https://n.test/1",
                text="第四层带丑女，详见 https://n.test/more 和 [原帖](https://n.test/post)",
                site="九游网",
                date="2021-06-10",
            ),
            WebPage(title="已有", url="https://n.test/old", text="旧的"),
            WebPage(title="百家号：阵容推荐", url="https://n.test/2", text="阵容", site="百家号"),
        ]

    source = WebSearchSource(search, topic="阴阳师")
    assert "「阴阳师」" in source.keywords_hint
    found = await source.collect("永生之海怎么打", " 永生之海 阵容 ", {"https://n.test/old"})
    assert asked == ["阴阳师 永生之海 阵容"]  # 搜索词里没有游戏名：加在前面
    # 网页的链接发不出去（平台只收报备过的域名）：资料里不带链接，内容里的网址也去掉，链接只用来去重
    assert [(e.title, e.url, e.text, e.date, e.key) for e in found] == [
        (
            "永生之海攻略（九游网）",
            "",
            "第四层带丑女，详见  和 原帖",
            "2021-06-10",
            "https://n.test/1",
        ),
        ("百家号：阵容推荐", "", "阵容", "", "https://n.test/2"),
    ]
    assert {(e.origin, e.kind) for e in found} == {("联网搜索", "网页")}
    assert "链接" not in render(found)

    await source.collect("阴阳师永生之海怎么打", "", set())
    assert asked[-1] == "阴阳师永生之海怎么打"  # 没给搜索词就用问题；已经有游戏名的不重复加


def test_plugin_builds_the_levels_that_are_available() -> None:
    repo = load_config(FULL_CONFIG_DIR)
    assert repo.get("core.plugins")["enabled"].index("research") > repo.get("core.plugins")[
        "enabled"
    ].index("knowledge")
    files = {
        "plugin.research": repo.plugin("research"),
        "core.websearch": repo.get("core.websearch"),
        "core.wiki": repo.get("core.wiki"),  # 这个 wiki 叫什么：提示词和联网搜索的搜索词里用
    }
    config = AppConfig(root=Path("."), files=files, secrets={})
    profiles = {
        name: SimpleNamespace(thinking=bool(item.get("thinking")))
        for name, item in repo.get("core.llm")["profiles"].items()
    }
    llm = SimpleNamespace(settings=SimpleNamespace(profiles=profiles))
    retriever = Retriever(MemoryKnowledgeStore(), None)

    ctx = PluginContext("research", config, {"llm": llm})
    ResearchPlugin().register(ctx)
    assert "research" not in ctx.services  # 没有知识库就不生效

    wiki = SimpleNamespace(fulltext_titles=None)
    ctx = PluginContext(
        "research", config, {"llm": llm, "knowledge": retriever, "wiki_query_client": wiki}
    )
    ResearchPlugin().register(ctx)
    flow: ResearchWorkflow = ctx.services["research"]
    # 联网搜索在配置里关着：跳过
    assert repo.get("core.websearch")["enabled"] is False and "web_search" not in ctx.services
    assert [source.name for source in flow.sources] == ["kb", "site"]
    # 知识库和站内搜索一起查，是同一级
    assert [[source.name for source in level] for level in flow.levels] == [["kb", "site"]]
    # 查证员、复核员都用不开思考的那一档（比过：答对的题数分不出高下，用时短得多）
    assert (flow.settings.analyst_profile, flow.settings.reviewer_profile) == ("quick", "quick")
    assert (flow.settings.max_rejections, flow.settings.max_seconds) == (2, 100.0)
    limits = flow.settings
    # 不开思考的调用给 60 秒（挑中的页面给整页以后一次要读几万字），查证员卡住了再问一次
    assert (limits.quick_timeout_seconds, limits.quick_retries) == (60.0, 1)
    # 最坏情况也在最后期限里：分析问题、查证员问两次、复核一次，每次都卡到限时；
    # 最后期限留出了群聊 5 分钟窗口里的余量
    first_round = limits.analysis_timeout_seconds + limits.quick_timeout_seconds * (
        limits.quick_retries + 2
    )
    assert first_round < limits.deadline_seconds
    assert limits.think_timeout_seconds + limits.quick_timeout_seconds < limits.deadline_seconds
    assert limits.deadline_seconds + repo.plugin("chat")["entry_timeout_seconds"] < 300
    assert flow.settings.answer_chars == 300
    # 分析问题用便宜的那一档，没法再走一轮时用不开思考的那一档修正
    assert (flow.settings.analysis_profile, flow.settings.quick_profile) == ("quick", "quick")
    assert flow.settings.analysis_topics and flow.settings.max_images == 2
    # 提示词里因站而异的说法：站名来自 core.wiki，其余的由站点插件补进 plugin.research
    # （这里没有站点插件）
    wording = flow.settings.wording
    assert (wording.bot, wording.intro) == ("示例 wiki 机器人", "示例 wiki")
    assert flow.name_lookup is not None and flow.image_search is None
    assert isinstance(flow.entry_pages, EntryPages) and flow.entry_pages.total == 4
    # 挑中的页面补成整页：我们的配置选全量（挑中一段就给整页、不限页面长短），资料上限也相应放大
    assert isinstance(flow.page_filler, PageFiller) and flow.page_filler.min_hits == 1
    assert flow.page_filler.max_chars > 10**6 and flow.settings.evidence_chars >= 100000
    # 一开始报一句，之后只在升级检索、复核、打回重写时报；一两秒就完的阶段（以图搜图、查知识库、
    # 快速修正）不单独报，单聊的 3 句名额里才留得下「正在复核…」
    assert set(flow.settings.progress) == {"analyze", "site", "web", "review", "revise"}
    assert flow.settings.progress["analyze"] == "正在查资料、整理答案…"
    assert flow.settings.progress["review"] == "正在复核…"
    assert bool(flow.settings.analysis_profile)  # 第一句挂在分析问题这一步上，这一步得开着
    for profile in ("quick", "reason", "review"):
        assert profile in repo.get("core.llm")["profiles"]
    # 哪些档位不开思考是从 core.llm.yaml 读出来的：角色配成这些档位时只等短的那个限时
    assert "quick" in flow.settings.fast_profiles and "chat" not in flow.settings.fast_profiles

    # 不写 mode 时是节选（只补挑中两段以上、不太长的页面）；off 是不补；写错了按节选
    for mode, limits in (
        (None, (2, 8000)),
        ("excerpt", (2, 8000)),
        ("off", None),
        ("错", (2, 8000)),
    ):
        section = {"min_hits": 2, "max_chars": 8000} | ({"mode": mode} if mode else {})
        files["plugin.research"] = {**repo.plugin("research"), "whole_page": section}
        ctx = PluginContext("research", config, {"llm": llm, "knowledge": retriever})
        ResearchPlugin().register(ctx)
        filler = ctx.services["research"].page_filler
        assert (filler and (filler.min_hits, filler.max_chars)) == limits
    files["plugin.research"] = repo.plugin("research")

    # 打开而且有密钥：联网搜索是最后一级，客户端登记成服务（退出时由宿主关连接），游戏名从配置里来
    files["core.websearch"] = {**files["core.websearch"], "enabled": True}
    for secrets in ({}, {"gitee_ai": {"api_key": "k"}}):  # 打开了但没填密钥：还是没有这一级
        config = AppConfig(root=Path("."), files=files, secrets=secrets)
        ctx = PluginContext("research", config, {"llm": llm, "knowledge": retriever})
        ResearchPlugin().register(ctx)
        assert ("web_search" in ctx.services) is bool(secrets)
    images = object()
    ctx = PluginContext(
        "research", config, {"llm": llm, "knowledge": retriever, "image_search": images}
    )
    ResearchPlugin().register(ctx)
    flow = ctx.services["research"]
    assert [source.name for source in flow.sources] == ["kb", "web"]
    web = flow.sources[1]
    assert isinstance(web, WebSearchSource) and web.topic == "示例"
    assert isinstance(ctx.services["web_search"], WebSearchClient)
    # 站内搜索用不了（没有 wiki_query）：那一级只剩知识库
    assert [[source.name for source in level] for level in flow.levels] == [["kb"], ["web"]]
    assert flow.name_lookup is None and flow.image_search is images
