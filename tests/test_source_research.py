"""通用来源研究闭环：内存存储、固定时钟、假嵌入与假模型，零外网。"""

import asyncio
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from jianshanagent.core.config import AppConfig
from jianshanagent.core.embedding import EmbeddingProvider
from jianshanagent.core.plugin import PluginContext
from jianshanagent.plugins.knowledge.news import NewsQuery, NewsResult
from jianshanagent.plugins.knowledge.retrieval import RetrievalSettings, Retriever
from jianshanagent.plugins.knowledge.source_filter import SourceFilter
from jianshanagent.plugins.knowledge.store import KbChunk, MemoryKnowledgeStore
from jianshanagent.plugins.research.analysis import (
    Analysis,
    NameNote,
    NewsSource,
    explicit_source_scope,
    source_filter_for,
    understand,
)
from jianshanagent.plugins.research.evidence import (
    EntryPages,
    KnowledgeSource,
    PageFiller,
    SiteSearchSource,
    _from_chunk,
    deduplicate_evidence,
    render,
)
from jianshanagent.plugins.research.plugin import (
    CurrentSourceRoles,
    ResearchPlugin,
    news_sources_from,
)
from jianshanagent.plugins.research.workflow import Decision, ResearchWorkflow, WorkflowSettings
from test_research import FakeSource, ScriptedLLM, ev

NOW = datetime(2026, 11, 26, 12, tzinfo=ZoneInfo("Asia/Shanghai"))


def config(tmp_path: Path, *, wiki=True, docs=True):
    return AppConfig(
        tmp_path / "config",
        {
            "core.wiki": {
                "name": "测试",
                "wikis": [
                    {"id": "wiki", "label": "甲Wiki", "api_url": "https://wiki.invalid/api.php"},
                ]
                if wiki
                else [],
            },
            "core.sources": {
                "sources": [
                    {"id": "alpha", "label": "甲微博", "platform": "weibo", "role": "official"},
                    {"id": "beta", "label": "乙微博", "platform": "weibo", "role": "community"},
                ]
                if docs
                else []
            },
            "plugin.research": {
                "enabled": True,
                "analysis": {"enabled": True},
                "profiles": {"analysis": "quick", "analyst": "reason", "reviewer": "review"},
                "whole_page": {"mode": "off"},
                "stages": ["kb"],
            },
        },
        {},
    )


def chunk(source, post="100", *, platform="weibo", seq=0, text="技能机制资料", role="official"):
    return KbChunk(
        chunk_id=f"{source}:{post}:{seq}",
        doc_key=f"{source}:weibo:{post}",
        kind="post",
        title=f"测试帖子{post}",
        text=text,
        url=f"https://example.invalid/{source}/{post}",
        wiki=source,
        seq=seq,
        date="2026-11-24",
        meta={
            "source_platform": platform,
            "source_role": role,
            "post_id": post,
            "canonical_document_id": f"weibo:{post}",
            "author_id": "author-a",
            "author_name": "甲作者",
        },
    )


async def memory(*parts):
    store = MemoryKnowledgeStore()
    for source in dict.fromkeys(part.wiki for part in parts):
        await store.sync(
            [(part, [1, 0]) for part in parts if part.wiki == source],
            wiki=source,
            fingerprint="test:test:2",
        )
    return store


class Embedder:
    provider = EmbeddingProvider("test", "http://invalid", "test", dimensions=2)

    def __init__(self):
        self.calls = 0

    async def embed_one(self, text):
        self.calls += 1
        return [1, 0]


def flow(llm, sources, registry, **kwargs):
    return ResearchWorkflow(
        llm,
        sources,
        WorkflowSettings(
            analysis_profile="quick",
            news_sources=registry,
        ),
        clock=lambda: NOW,
        **kwargs,
    )


def test_registered_instance_is_not_original_platform_and_legacy_aliases_stay(tmp_path):
    cfg = config(tmp_path)
    registry = news_sources_from(cfg, {})
    assert registry["source:wiki"].source_ids == ("wiki",)
    assert registry["source:wiki"].source_platform == ""
    assert registry["weibo"].source_platform == "weibo"
    assert registry["weibo"].source_ids is None
    assert registry["source:alpha"].aliases == ("alpha",)
    old = {"weibo": {"label": "官博", "source_platform": "weibo", "aliases": ["扫地工微博"]}}
    only_wiki = news_sources_from(config(tmp_path, docs=False), old)
    assert only_wiki["weibo"].aliases == ("扫地工微博",)


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("甲微博的技能机制怎么计算", ("source:alpha",)),
        ("alpha提到的技能怎么计算", ("source:alpha",)),
        ("alphabeta是什么", ()),
        ("甲微博与乙微博最近发了什么", ("source:alpha", "source:beta")),
        ("甲微博以及微博最近发了什么", ("weibo", "source:alpha")),
        ("微博之夜怎么参加", ()),
        ("「甲微博」是哪个活动名字", ()),
    ],
)
def test_specific_alias_beats_nested_generic(tmp_path, question, expected):
    assert explicit_source_scope(question, news_sources_from(config(tmp_path), {})) == expected


async def test_lookup_only_honors_explicit_user_scope(tmp_path):
    registry = news_sources_from(config(tmp_path), {})
    llm = ScriptedLLM([], analysis=Analysis(topic="技能", source="weibo", names=["角色"]))
    ordinary = await understand(llm, "角色技能机制是什么", profile="quick", news_sources=registry)
    assert ordinary.intent == "lookup" and ordinary.source_scope == ()
    scoped = await understand(
        llm, "甲微博里角色技能机制是什么", profile="quick", news_sources=registry
    )
    assert scoped.intent == "lookup" and scoped.source_scope == ("source:alpha",)
    assert "用户明确限定资料来源" in " ".join(scoped.notes())


def test_mixed_selector_uses_or_not_cross_product():
    registry = {
        "wiki": NewsSource("甲Wiki", (), source_ids=("wiki",)),
        "account": NewsSource("作者", (), "weibo", source_ids=("alpha",), author_ids=("author-a",)),
    }
    selected = source_filter_for(("wiki", "account"), registry)
    wiki_native = chunk("wiki", platform="")
    alpha_post = chunk("alpha")
    wrong_author = replace(alpha_post, meta={**alpha_post.meta, "author_id": "author-b"})
    assert selected.allows(wiki_native)
    assert selected.allows(alpha_post)
    assert not selected.allows(wrong_author)
    assert not selected.allows(chunk("beta"))
    sql, args = selected.sql("c")
    assert " OR " in sql and "author-a" in str(args)
    assert not source_filter_for(("missing",), registry).allows(alpha_post)
    assert not source_filter_for((), registry).allows(alpha_post)


async def test_actual_news_or_accepts_native_wiki_and_direct_weibo(tmp_path):
    registry = news_sources_from(config(tmp_path), {})
    native = replace(chunk("wiki", "101", platform=""), kind="announcement")
    direct = chunk("alpha", "102")
    excluded = chunk("beta", "103")
    store = await memory(native, direct, excluded)
    selected = source_filter_for(("source:wiki", "source:alpha"), registry)
    result = await store.recent_documents(NewsQuery(source_platforms=None, source_filter=selected))
    assert {part.doc_key for part in result.chunks} == {native.doc_key, direct.doc_key}


async def test_generic_weibo_queries_mirror_plus_direct_and_deduplicates(tmp_path):
    mirror, direct, extra = chunk("wiki"), chunk("alpha"), chunk("beta", "200")
    native = replace(chunk("wiki", "300", platform=""), kind="announcement")
    store = await memory(mirror, direct, extra, native)
    registry = news_sources_from(config(tmp_path), {})
    result = await store.recent_documents(
        NewsQuery(
            source_platforms=None,
            source_filter=source_filter_for(("weibo",), registry),
        )
    )
    assert result.matching_documents == 2
    assert {part.meta["canonical_document_id"] for part in result.chunks} == {
        "weibo:100",
        "weibo:200",
    }


async def test_scoped_lookup_skips_unfiltered_entry_site_and_legacy_filler(tmp_path):
    registry = news_sources_from(config(tmp_path), {})
    store = await memory(chunk("alpha"), chunk("beta", "200"))
    retriever = Retriever(store, Embedder(), RetrievalSettings(max_df_ratio=1.0))
    entry = FakeSource("entries", "条目页", [ev("leak-entry", "不允许条目内容" * 100)])
    site = FakeSource("site", "站内搜索", [ev("leak-site", "其它来源")])

    class LegacyFiller:
        called = False

        async def fill(self, *args):
            self.called = True
            return [ev("leak-fill", "无过滤补全")]

    filler = LegacyFiller()
    llm = ScriptedLLM([Decision(answer="甲微博提到机制。")], analysis=Analysis(names=["角色"]))

    async def lookup(name):
        return [NameNote(name, name, "角色")]

    outcome = await flow(
        llm,
        [[KnowledgeSource(retriever), site]],
        registry,
        entry_pages=entry,
        name_lookup=lookup,
        page_filler=filler,
    ).run("甲微博角色技能机制是什么")
    assert {item.provenance["source_id"] for item in outcome.evidence} == {"alpha"}
    assert entry.asked == site.asked == [] and not filler.called
    assert "不允许条目内容" not in llm.analyst_prompts[0]


async def test_scoped_page_filler_never_fetches_other_namespace(tmp_path):
    a0, a1, b0 = chunk("alpha"), chunk("alpha", seq=1), chunk("beta")
    store = await memory(a0, a1, b0)
    retriever = Retriever(store, Embedder())
    roles = {"alpha": "community", "beta": "official"}
    filler = PageFiller(retriever, source_roles=roles)
    initial = [
        _from_chunk(a0, "资料库", source_roles=roles),
        _from_chunk(b0, "资料库", source_roles=roles),  # 调用方旧候选越界也不能保留。
    ]
    filled = await filler.fill_filtered(
        initial, 0, 10000, SourceFilter(source_ids=("alpha",)), news=True
    )
    assert {item.key for item in filled} == {a0.chunk_id, a1.chunk_id}
    assert all(item.provenance["source_role"] == "community" for item in filled)
    retriever.exclude_wikis = frozenset({"alpha"})
    # 停用发生后不能再由旧候选补回完整文档。
    filled_after = await filler.fill_filtered(
        initial, 0, 10000, SourceFilter(source_ids=("alpha",)), news=True
    )
    assert filled_after == []


async def test_request_filters_do_not_mutate_shared_source(tmp_path):
    registry = news_sources_from(config(tmp_path), {})
    seen = []

    class SharedSource:
        name, label, keywords_hint = "kb", "知识库", ""

        async def collect_filtered(self, question, keywords, have, source_filter):
            seen.append(source_filter)
            await asyncio.sleep(0)
            source = source_filter.source_ids[0]
            return [_from_chunk(chunk(source), "知识库")]

        async def collect(self, *args):
            return [ev("unscoped", "无范围普通资料")]

    shared = SharedSource()
    one = ScriptedLLM([Decision(answer="甲")], analysis=Analysis())
    two = ScriptedLLM([Decision(answer="乙")], analysis=Analysis())
    a, b = await asyncio.gather(
        flow(one, [shared], registry).run("甲微博技能机制是什么"),
        flow(two, [shared], registry).run("乙微博技能机制是什么"),
    )
    assert {item.provenance["source_id"] for item in a.evidence} == {"alpha"}
    assert {item.provenance["source_id"] for item in b.evidence} == {"beta"}
    assert {item.source_ids for item in seen} == {("alpha",), ("beta",)}


async def test_news_default_registry_passes_or_filter_not_flat_platform_union(tmp_path):
    registry = news_sources_from(config(tmp_path), {})
    seen = []

    class News:
        name, label, keywords_hint = "kb", "知识库", ""

        async def collect_news(self, query):
            seen.append(query)
            return [], NewsResult([])

    llm = ScriptedLLM([Decision(answer="暂无收录")], analysis=Analysis(intent="news_overview"))
    await flow(llm, [News()], registry).run("最近有哪些活动新动态")
    assert seen[0].source_platforms is None
    assert seen[0].source_filter.any_of
    assert seen[0].source_filter.allows(chunk("wiki", platform=""))
    assert seen[0].source_filter.allows(chunk("alpha"))


async def test_source_scope_survives_disabled_model_analysis(tmp_path):
    registry = news_sources_from(config(tmp_path), {})
    store = await memory(chunk("alpha"), chunk("beta", "200"))
    llm = ScriptedLLM([Decision(answer="仅甲")])
    workflow = ResearchWorkflow(
        llm,
        [KnowledgeSource(Retriever(store, Embedder()))],
        WorkflowSettings(news_sources=registry),
    )
    outcome = await workflow.run("甲微博技能怎么计算")
    assert {item.provenance["source_id"] for item in outcome.evidence} == {"alpha"}


def test_evidence_dedup_keeps_multichunks_reposts_and_capture_urls():
    first, second, mirror = chunk("alpha"), chunk("alpha", seq=1), chunk("wiki")
    repost = chunk("beta", "200")
    repost.meta.update(origin_id="100", repost_of="weibo:100", source_role="community")
    evidence = [_from_chunk(part, "资料库") for part in (first, second, mirror, repost)]
    kept = deduplicate_evidence(evidence)
    assert [item.key for item in kept] == [first.chunk_id, second.chunk_id, repost.chunk_id]
    assert {entry["source_id"] for entry in kept[0].provenance["captures"]} == {"alpha", "wiki"}
    rendered = render(kept)
    assert "引用原帖 100" in rendered and "转述不算另一份独立佐证" in rendered
    assert "社区/玩家讨论" in rendered
    assert "本帖身份：weibo:200" in rendered


async def test_evidence_pool_enforces_cross_source_dedup():
    parts = [chunk("alpha"), chunk("alpha", seq=1), chunk("wiki"), chunk("beta", "200")]
    source = FakeSource("kb", "知识库", [_from_chunk(part, "资料库") for part in parts])
    llm = ScriptedLLM([Decision(answer="三片而非四份独立证据")])
    outcome = await ResearchWorkflow(llm, [source]).run("技能是什么")
    assert len(outcome.evidence) == 3
    assert len(outcome.evidence[0].provenance["captures"]) == 2


async def test_current_role_demotion_overrides_old_imported_official_meta(tmp_path):
    cfg = config(tmp_path)
    old = chunk("alpha", role="official")
    store = await memory(old)
    source = KnowledgeSource(Retriever(store, Embedder()), source_roles=CurrentSourceRoles(cfg))
    first = await source.collect("技能", "", set())
    assert first[0].provenance["source_role"] == "official"
    cfg.files["core.sources"]["sources"][0]["role"] = "community"
    changed = await source.collect("技能", "", set())
    assert changed[0].provenance["source_role"] == "community"
    assert old.meta["source_role"] == "official"  # 不改正文或历史存储元信息。
    assert "社区/玩家讨论" in render(changed)
    assert "登记为官方来源" not in render(changed)


async def test_entry_and_site_attach_current_roles():
    part = chunk("alpha", role="official")

    class RetrieverFake:
        async def search_pages(self, *args, **kwargs):
            return [part]

    async def search(*args):
        return {"alpha": [part.title]}

    retriever = RetrieverFake()
    roles = {"alpha": "unknown"}
    entries = await EntryPages(retriever, source_roles=roles).collect("技能", [part.title], set())
    site = await SiteSearchSource(retriever, search, source_roles=roles).collect(
        "技能", "词", set()
    )
    assert entries[0].provenance["source_role"] == site[0].provenance["source_role"] == "unknown"


async def test_no_wiki_plugin_with_fake_llm_and_real_hybrid_joint_evidence(tmp_path):
    cfg = config(tmp_path, wiki=False)
    store = await memory(chunk("alpha"), chunk("beta", "200", role="community"))
    embedder = Embedder()
    retriever = Retriever(store, embedder, RetrievalSettings(max_df_ratio=1.0))
    llm = ScriptedLLM(
        [Decision(answer="甲乙来源都有技能资料。", sources=[1, 2])],
        analysis=Analysis(topic="技能", names=[]),
    )
    llm.settings = SimpleNamespace(
        profiles={
            "quick": SimpleNamespace(thinking=False),
            "reason": SimpleNamespace(thinking=True),
            "review": SimpleNamespace(thinking=False),
        }
    )
    ctx = PluginContext("research", cfg, {"llm": llm, "knowledge": retriever})
    ResearchPlugin().register(ctx)
    workflow = ctx.services["research"]
    workflow.clock = lambda: NOW
    outcome = await workflow.run("技能机制资料有哪些")
    assert workflow.settings.wording.bot == "测试资料库助手"
    assert workflow.settings.wording.intro == "测试资料库"
    assert workflow.name_lookup is None
    assert {item.source for item in outcome.evidence} == {"甲微博", "乙微博"}
    assert len(outcome.cited) == 2 and embedder.calls == 1
    assert "社区/玩家讨论" in llm.analyst_prompts[0]
    assert "其余的是 wiki 收录" not in llm.analyst_prompts[0]


def test_single_wiki_keeps_legacy_unlabelled_evidence_behavior(tmp_path):
    cfg = config(tmp_path, docs=False)
    llm = SimpleNamespace(
        settings=SimpleNamespace(
            profiles={
                "quick": SimpleNamespace(thinking=False),
                "reason": SimpleNamespace(thinking=True),
                "review": SimpleNamespace(thinking=False),
            }
        )
    )
    ctx = PluginContext(
        "research", cfg, {"llm": llm, "knowledge": Retriever(MemoryKnowledgeStore(), None)}
    )
    ResearchPlugin().register(ctx)
    assert ctx.services["research"].sources[0].sources == {}


async def test_unscoped_lookup_keeps_entry_site_and_legacy_fill_paths(tmp_path):
    registry = news_sources_from(config(tmp_path), {})
    store = await memory(chunk("alpha"), chunk("beta", "200"))
    retriever = Retriever(store, Embedder(), RetrievalSettings(max_df_ratio=1.0))
    entry = FakeSource("entries", "条目页", [ev("entry-own", "优先读取角色自己的条目")])
    site = FakeSource("site", "站内搜索", [ev("site-extra", "站内补充资料")])

    class LegacyFiller:
        called = False

        async def fill(self, evidence, start, room):
            self.called = True
            return list(evidence)

    filler = LegacyFiller()

    async def lookup(name):
        return [NameNote(name, name, "角色")]

    # 模型自行猜微博不应约束用户未提来源的普通技能问题。
    llm = ScriptedLLM(
        [Decision(answer="机制解释")],
        analysis=Analysis(
            topic="技能",
            source="weibo",
            names=["角色"],
            keywords=["角色"],
        ),
    )
    outcome = await flow(
        llm,
        [[KnowledgeSource(retriever), site]],
        registry,
        entry_pages=entry,
        name_lookup=lookup,
        page_filler=filler,
    ).run("角色技能机制是什么")
    assert outcome.evidence[0].key == "entry-own"
    assert len(entry.asked) == len(site.asked) == 1
    assert filler.called
    assert {item.provenance.get("source_id") for item in outcome.evidence} >= {"alpha", "beta"}
    assert "site-extra" in {item.key for item in outcome.evidence}


def test_missing_current_registration_never_keeps_stale_official_role():
    old = chunk("removed", role="official")
    assert _from_chunk(old, "资料库").provenance["source_role"] == "official"  # 旧调用默认不变。
    assert _from_chunk(old, "资料库", source_roles={}).provenance["source_role"] == "unknown"


def test_configured_author_selector_can_leave_original_platform_unrestricted(tmp_path):
    registry = news_sources_from(
        config(tmp_path),
        {
            "author": {"label": "作者账号", "author_ids": ["author-a"], "source_ids": ["alpha"]},
        },
    )
    selected = source_filter_for(("author",), registry)
    assert selected.source_platforms is None
    assert selected.allows(chunk("alpha", platform=""))
    assert not selected.allows(chunk("beta"))


async def test_scoped_workflow_uses_filtered_filler_and_keeps_news_semantics(tmp_path):
    registry = news_sources_from(config(tmp_path), {})
    a0, a1 = chunk("alpha"), chunk("alpha", seq=1, text="正文后半片")
    store = await memory(a0, a1, chunk("beta", "200"))
    retriever = Retriever(store, Embedder())
    filler = PageFiller(retriever, min_hits=2)
    llm = ScriptedLLM(
        [Decision(answer="甲微博的完整帖子")], analysis=Analysis(intent="news_overview")
    )
    outcome = await flow(llm, [KnowledgeSource(retriever)], registry, page_filler=filler).run(
        "甲微博最近发了什么"
    )
    assert {item.key for item in outcome.evidence} == {a0.chunk_id, a1.chunk_id}
    assert all(item.provenance["source_id"] == "alpha" for item in outcome.evidence)
