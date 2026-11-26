"""来源隔离、同原帖跨采集去重；只用Memory或显式本地TEST_DSN。"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Sequence
from dataclasses import replace
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock
from urllib.parse import parse_qsl, urlencode, urlsplit
from uuid import uuid4

import asyncpg
import pytest

from jianshanagent.core.db import MIGRATIONS_DIR, Database
from jianshanagent.core.embedding import EmbeddingClient, EmbeddingProvider
from jianshanagent.plugins.knowledge.news import NewsQuery
from jianshanagent.plugins.knowledge.retrieval import RetrievalSettings, Retriever
from jianshanagent.plugins.knowledge.source_filter import (
    SourceFilter,
    canonical_document_id,
    provenance,
)
from jianshanagent.plugins.knowledge.store import (
    KbChunk,
    KnowledgeStore,
    MemoryKnowledgeStore,
    PostgresKnowledgeStore,
)

DIM = 4096
PROVIDER = EmbeddingProvider(
    name="source-test", base_url="http://unused.test/v1", model="m", dimensions=DIM
)
FP = PROVIDER.fingerprint


def post(
    source: str,
    identity: str,
    *,
    seq: int = 0,
    author: str = "alice",
    published: str | None = "2026-05-20",
    legacy: bool = False,
    text: str = "测试正文",
) -> KbChunk:
    return KbChunk(
        chunk_id=f"{source}:weibo:{identity}:{seq}",
        doc_key=f"{source}:weibo:{identity}",
        wiki=source,
        kind="announcement" if legacy else "post",
        title="测试帖子",
        text=text,
        seq=seq,
        date=published,
        url=f"https://weibo.example/{identity}",
        meta={
            "source_platform": "weibo",
            "author_id": author,
            **({} if legacy else {"canonical_document_id": f"weibo:{identity}"}),
        },
    )


@pytest.fixture(params=["memory", "postgres"])
async def source_store(request: pytest.FixtureRequest) -> AsyncIterator[KnowledgeStore]:
    if request.param == "memory":
        yield MemoryKnowledgeStore()
        return
    dsn = os.environ.get("TEST_DSN")
    if not dsn:
        pytest.skip("未设置独立本地TEST_DSN")
    if urlsplit(dsn).hostname not in (None, "localhost", "127.0.0.1", "::1"):
        pytest.skip("禁止连接外网或生产数据库")
    schema = f"test_sources_{uuid4().hex}"
    address, _, params = dsn.partition("?")
    settings = dict(parse_qsl(params, keep_blank_values=True))
    settings["search_path"] = f"{schema},public"
    db = Database(f"{address}?{urlencode(settings)}")
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(f"CREATE SCHEMA {schema}")
        await db.connect()
        await db.pool.execute((MIGRATIONS_DIR / "0003_knowledge.sql").read_text())
        await db.pool.execute("ALTER TABLE kb_chunks ADD COLUMN wiki TEXT NOT NULL DEFAULT ''")
        yield PostgresKnowledgeStore(db)
    finally:
        await db.close()
        try:
            await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        finally:
            await conn.close()


async def load(store: KnowledgeStore, chunks: Sequence[KbChunk], *, vectors: bool = False) -> None:
    for source in sorted({c.wiki for c in chunks}):
        await store.sync(
            [
                (c, [1.0 - c.seq / 10] + [0.0] * (DIM - 1) if vectors else None)
                for c in chunks
                if c.wiki == source
            ],
            fingerprint=FP,
            wiki=source,
        )


def test_canonical_is_numeric_original_platform_identity_not_title_or_origin() -> None:
    legacy = post("huiji", "536123", legacy=True)
    assert legacy.doc_key == "huiji:weibo:536123"
    assert canonical_document_id(legacy) == "weibo:536123"
    assert canonical_document_id(post("direct", "536123")) == "weibo:536123"
    assert canonical_document_id(replace(legacy, meta={"source_platform": "wiki"})) is None
    assert canonical_document_id(replace(legacy, doc_key="huiji:weibo:not-a-number")) is None
    assert (
        canonical_document_id(
            replace(
                legacy,
                doc_key="huiji:anything",
                meta={
                    "source_platform": "weibo",
                    "origin_id": "weibo:536123",
                },
            )
        )
        is None
    )
    assert canonical_document_id(post("direct", "789")) != canonical_document_id(legacy)
    assert provenance(legacy)["source_role"] == "unknown"
    assert legacy.source_id == "huiji"


def test_filters_none_empty_or_and_exclusion_are_not_confused() -> None:
    a = post("wiki", "1")
    b = post("direct", "2", author="bob")
    assert SourceFilter().allows(a)
    assert not SourceFilter(source_ids=()).allows(a)
    assert not SourceFilter(source_platforms=()).allows(a)
    assert not SourceFilter(author_ids=()).allows(a)
    either = SourceFilter(
        any_of=(SourceFilter(source_ids=("wiki",)), SourceFilter(author_ids=("bob",)))
    )
    assert either.allows(a) and either.allows(b)
    restricted = either.intersect(SourceFilter(exclude_source_ids=("wiki",)))
    assert not restricted.allows(a) and restricted.allows(b)
    both = either.intersect(SourceFilter(any_of=(SourceFilter(author_ids=("alice",)),)))
    assert both.allows(a) and not both.allows(b)
    sql, args = restricted.sql("c", 4)
    assert " OR " in sql and " AND " in sql and "'wiki'" not in sql
    assert "$4::text[]" in sql and all(not isinstance(arg, str) for arg in args)


async def test_recent_groups_after_source_date_author_and_before_limit(
    source_store: KnowledgeStore,
) -> None:
    mirror = post("huiji", "100", legacy=True, author="alice", published="2026-05-21")
    direct = post("direct", "100", author="bob", published="2026-05-20")
    other = post("direct", "200", author="bob", published="2026-05-19")
    repost = replace(
        post("direct", "300", author="bob", published="2026-05-18"),
        meta={**post("direct", "300").meta, "author_id": "bob", "origin_id": "weibo:100"},
    )
    await load(source_store, [mirror, direct, other, repost])
    all_posts = await source_store.recent_documents(
        NewsQuery(("weibo",), as_of="2026-05-22", limit=2)
    )
    assert all_posts.matching_documents == 3
    assert [canonical_document_id(c) for c in all_posts.chunks] == ["weibo:100", "weibo:200"]
    assert all_posts.chunks[0].doc_key == mirror.doc_key
    assert {c["source_id"] for c in provenance(all_posts.chunks[0])["captures"]} == {
        "huiji",
        "direct",
    }
    filtered = await source_store.recent_documents(
        NewsQuery(
            ("weibo",),
            as_of="2026-05-22",
            author_ids=("bob",),
            date_to="2026-05-20",
            limit=1,
        )
    )
    assert filtered.chunks[0].doc_key == direct.doc_key
    assert filtered.matching_documents == 3 and filtered.source_latest_date == "2026-05-20"
    assert len(provenance(filtered.chunks[0])["captures"]) == 1
    hidden = await source_store.recent_documents(
        NewsQuery(
            ("weibo",),
            as_of="2026-05-22",
            source_ids=("direct",),
        ),
        exclude_wikis=("direct",),
    )
    assert hidden.chunks == [] and hidden.matching_documents == 0
    restored = await source_store.recent_documents(
        NewsQuery(None, as_of="2026-05-22", source_ids=("huiji",))
    )
    assert [c.doc_key for c in restored.chunks] == [mirror.doc_key]
    assert (await source_store.info()).chunks == 4
    assert (await source_store.recent_documents(NewsQuery((), as_of="2026-05-22"))).chunks == []


async def test_candidates_and_all_completion_paths_respect_same_filter(
    source_store: KnowledgeStore,
) -> None:
    chunks = [post("hidden", "1", seq=0), post("visible", "2", seq=1), post("visible", "2", seq=2)]
    await load(source_store, chunks, vectors=True)
    f = SourceFilter(source_ids=("visible",), author_ids=("alice",), source_platforms=("weibo",))
    v = [1.0] + [0.0] * (DIM - 1)
    assert (await source_store.vector_search(v, limit=1, source_filter=f))[0].chunk == chunks[1]
    assert (await source_store.keyword_search(("测试",), limit=1, max_df_ratio=1, source_filter=f))[
        0
    ].chunk == chunks[1]
    assert await source_store.get([c.chunk_id for c in chunks], source_filter=f) == chunks[1:]
    assert [
        c.chunk_id for c in await source_store.outline([chunks[0].chunk_id], source_filter=f)
    ] == []
    assert len(await source_store.page("测试帖子", source_filter=f)) == 2
    for method, kwargs in (
        (source_store.vector_search, {"vector": v, "limit": 1}),
        (source_store.keyword_search, {"terms": ("测试",), "limit": 1, "max_df_ratio": 1}),
    ):
        assert await method(**kwargs, source_filter=SourceFilter(source_ids=())) == []
    retriever = Retriever(source_store, None, exclude_wikis=("hidden",))
    assert [c.chunk_id for c in await retriever.get([c.chunk_id for c in chunks])] == [
        c.chunk_id for c in chunks[1:]
    ]
    assert (
        await retriever.page_text(
            "测试帖子", chars=100, source_filter=SourceFilter(source_ids=("hidden",))
        )
        == ""
    )
    retriever.exclude_wikis = frozenset(("visible", "hidden"))
    plan, filled = await retriever.fill_pages([c.chunk_id for c in chunks[1:]], room=4)
    assert not plan.missing and filled == []
    assert (await retriever.search("测试", source_filter=f)).hits == []
    assert await retriever.search_pages("测试", ["测试帖子"], limit=2, source_filter=f) == []


async def test_any_of_reaches_sql_before_limit(source_store: KnowledgeStore) -> None:
    wiki = replace(post("wiki", "1"), kind="announcement", meta={"source_platform": "wiki"})
    weibo = post("direct", "2", seq=1)
    hidden = post("hidden", "3", seq=0)
    await load(source_store, [wiki, weibo, hidden], vectors=True)
    either = SourceFilter(
        any_of=(SourceFilter(source_ids=("wiki",)), SourceFilter(source_platforms=("weibo",)))
    )
    f = either.intersect(SourceFilter(exclude_source_ids=("hidden",)))
    rows = await source_store.vector_search([1.0] + [0.0] * (DIM - 1), limit=2, source_filter=f)
    assert {r.chunk.wiki for r in rows} == {"wiki", "direct"}
    news = await source_store.recent_documents(NewsQuery(None, as_of="2026-05-22", source_filter=f))
    assert {c.wiki for c in news.chunks} == {"wiki", "direct"}


async def test_hybrid_copy_dedup_refills_but_keeps_selected_document_slices(
    source_store: KnowledgeStore,
) -> None:
    first = post("a", "100", seq=0)
    duplicate = post("b", "100", seq=0)
    second = post("a", "100", seq=1)
    other = post("c", "200", seq=2)
    await load(source_store, [first, duplicate, second, other], vectors=True)
    embedder = cast(
        EmbeddingClient,
        SimpleNamespace(
            provider=PROVIDER,
            embed_one=AsyncMock(
                return_value=[1.0] + [0.0] * (DIM - 1),
            ),
        ),
    )
    retriever = Retriever(source_store, embedder, RetrievalSettings(top_k=3, keyword_slots=0))
    found = await retriever.search("unrelatedquery")
    assert [h.chunk.chunk_id for h in found.hits] == [
        first.chunk_id,
        second.chunk_id,
        other.chunk_id,
    ]
    assert len(provenance(found.hits[0].chunk)["captures"]) == 2
    retriever.exclude_wikis = frozenset(("a",))
    found = await retriever.search("unrelatedquery")
    assert [h.chunk.chunk_id for h in found.hits] == [duplicate.chunk_id, other.chunk_id]
    assert (await source_store.info()).chunks == 4


async def test_legacy_namespace_belongs_only_to_first_wiki(source_store: KnowledgeStore) -> None:
    legacy = replace(post("old", "500"), wiki="", chunk_id="weibo:500:0", doc_key="weibo:500")
    direct = post("direct", "600")
    await load(source_store, [legacy, direct])
    retriever = Retriever(
        source_store,
        None,
        legacy_source_id="wiki",
        source_filter=SourceFilter(source_ids=("wiki", "direct")),
    )
    wiki_filter = SourceFilter(source_ids=("wiki",))
    assert await retriever.get([legacy.chunk_id, direct.chunk_id], source_filter=wiki_filter) == [
        legacy
    ]
    recent = await retriever.recent(NewsQuery(None, as_of="2026-05-22", source_ids=("wiki",)))
    assert [c.doc_key for c in recent.chunks] == [legacy.doc_key]
    retriever.exclude_wikis = frozenset(("wiki",))
    assert await retriever.get([legacy.chunk_id], source_filter=wiki_filter) == []
    zero_wiki = Retriever(source_store, None, exclude_wikis=("",))
    assert await zero_wiki.get([legacy.chunk_id, direct.chunk_id]) == [direct]


@pytest.mark.parametrize("forged", ["wiki", "chunk_id", "doc_key"])
async def test_sync_namespace_guard_preserves_existing_data(
    source_store: KnowledgeStore,
    forged: str,
) -> None:
    existing = post("a", "1")
    await load(source_store, [existing])
    bad = replace(post("b", "2"), **{forged: "a" if forged == "wiki" else "a:forged"})
    with pytest.raises(ValueError, match="来源"):
        await source_store.sync([(bad, None)], fingerprint=FP, wiki="b")
    assert await source_store.get([existing.chunk_id]) == [existing]
    assert (await source_store.info()).chunks == 1


async def test_registered_allowlist_constrains_requests_and_every_completion_path(
    source_store: KnowledgeStore,
) -> None:
    ghost = post("unregistered", "1", published="2026-05-22", text="未登记资料")
    disabled = post("disabled", "2", seq=1, published="2026-05-21", text="停用资料")
    visible = [
        post("visible", "3", seq=i, published="2026-05-20", text=f"有效资料{i}") for i in (2, 3, 4)
    ]
    await load(source_store, [ghost, disabled, *visible], vectors=True)
    embedder = cast(
        EmbeddingClient,
        SimpleNamespace(
            provider=PROVIDER,
            embed_one=AsyncMock(
                return_value=[1.0] + [0.0] * (DIM - 1),
            ),
        ),
    )
    allowlist = SourceFilter(source_ids=("visible", "disabled"))
    retriever = Retriever(
        source_store,
        embedder,
        RetrievalSettings(top_k=1, keyword_slots=0),
        source_filter=allowlist,
        exclude_wikis=("disabled",),
    )
    either = SourceFilter(
        any_of=(
            SourceFilter(source_ids=("unregistered",)),
            SourceFilter(source_platforms=("weibo",)),
        )
    )
    assert (await retriever.search("unrelated", source_filter=either)).hits[0].chunk == visible[0]
    assert (
        await Retriever(source_store, embedder, RetrievalSettings(top_k=1, keyword_slots=0)).search(
            "unrelated"
        )
    ).hits[0].chunk == ghost  # 旧直接构造仍不限来源。
    keyword_only = Retriever(
        source_store,
        None,
        RetrievalSettings(max_df_ratio=1),
        source_filter=allowlist,
        exclude_wikis=("disabled",),
    )
    assert {
        hit.chunk.wiki for hit in (await keyword_only.search("资料", source_filter=either)).hits
    } == {"visible"}
    recent = await retriever.recent(
        NewsQuery(None, as_of="2026-05-22", limit=1, source_filter=either)
    )
    assert recent.chunks[0].doc_key == visible[0].doc_key
    assert recent.source_latest_date == "2026-05-20" and recent.matching_documents == 1
    all_ids = [ghost.chunk_id, disabled.chunk_id, *(c.chunk_id for c in visible)]
    assert await retriever.get(all_ids, source_filter=either) == visible
    text = await retriever.page_text("测试帖子", chars=1000, source_filter=either)
    assert "有效资料" in text and "未登记资料" not in text and "停用资料" not in text
    assert [
        c.chunk_id
        for c in await retriever.search_pages(
            "unrelated",
            ["测试帖子"],
            limit=1,
            source_filter=either,
        )
    ] == [visible[0].chunk_id]
    forbidden = SourceFilter(source_ids=("unregistered",))
    assert (await retriever.search("unrelated", source_filter=forbidden)).hits == []
    assert (
        await retriever.recent(NewsQuery(None, as_of="2026-05-22", source_filter=forbidden))
    ).chunks == []
    assert await retriever.get(all_ids, source_filter=forbidden) == []
    assert await retriever.page_text("测试帖子", chars=1000, source_filter=forbidden) == ""
    assert (
        await retriever.search_pages("unrelated", ["测试帖子"], limit=1, source_filter=forbidden)
        == []
    )
    plan, filled = await retriever.fill_pages([ghost.chunk_id], room=5, min_hits=1)
    assert not plan.missing and filled == []
    plan, filled = await retriever.fill_pages([visible[0].chunk_id], room=100, min_hits=1)
    assert {c.chunk_id for c in filled} == {c.chunk_id for c in visible[1:]}
    assert all(c.wiki == "visible" for c in filled)
    assert (await source_store.info()).chunks == 5  # 未登记与停用数据都未删除。
    empty = Retriever(source_store, None, source_filter=SourceFilter(source_ids=()))
    assert await empty.get(all_ids) == []
    assert (await empty.recent(NewsQuery(None, as_of="2026-05-22"))).chunks == []


async def test_plugin_registers_retained_sources_without_granting_legacy_to_weibo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jianshanagent.plugins.knowledge import plugin

    store = MemoryKnowledgeStore()
    legacy = replace(post("old", "1"), wiki="", chunk_id="weibo:1:0", doc_key="weibo:1")
    retained = post("direct", "2")
    removed = post("removed", "3")
    disabled = post("disabled", "4")
    await load(store, [legacy, retained, removed, disabled])
    monkeypatch.setattr(plugin.EmbeddingSettings, "from_config", lambda config: None)
    monkeypatch.setattr(plugin, "PostgresKnowledgeStore", lambda db: store)
    monkeypatch.setattr(plugin, "retained_source_ids", lambda config: config.retained)
    monkeypatch.setattr(plugin, "disabled_source_ids", lambda config: config.disabled)
    monkeypatch.setattr(plugin, "disabled_wiki_ids", lambda config: set())
    initial_config = SimpleNamespace(
        wikis=[], retained={"direct", "disabled"}, disabled={"disabled"}
    )
    runtime = SimpleNamespace(config=initial_config)
    services: dict[str, object] = {"database": object(), "runtime": runtime}
    ctx = SimpleNamespace(
        config=initial_config,
        services=services,
        plugin_config=lambda: {"retrieval": {"enabled": True}},
        register_service=lambda name, service: services.__setitem__(name, service),
    )
    plugin.KnowledgePlugin().register(cast(plugin.PluginContext, ctx))
    retriever = cast(Retriever, services["knowledge"])
    assert retriever.source_filter == SourceFilter(source_ids=("direct", "disabled"))
    assert retriever.exclude_wikis == frozenset(("", "disabled"))
    assert await retriever.get(
        [legacy.chunk_id, retained.chunk_id, removed.chunk_id, disabled.chunk_id]
    ) == [retained]
    runtime.config = SimpleNamespace(
        wikis=[], retained={"direct", "disabled"}, disabled={"direct", "disabled"}
    )
    assert ctx.config is initial_config  # 旧host持有旧快照，provider必须读取runtime.config。
    assert await retriever.get([retained.chunk_id]) == []
    runtime.config = SimpleNamespace(wikis=[], retained={"removed"}, disabled=set())
    assert await retriever.get([retained.chunk_id, removed.chunk_id]) == []
    services.pop("runtime")
    assert await retriever.get([retained.chunk_id]) == [retained]  # 无runtime离线装配兼容。
    services["runtime"] = SimpleNamespace()  # 当前配置读取异常不得回退旧允许集合。
    assert await retriever.get([retained.chunk_id]) == []
    assert (await store.info()).chunks == 4


@pytest.mark.parametrize("change", ["disabled", "removed", "unavailable", "invalid"])
async def test_same_retriever_obeys_live_source_scope_for_every_later_read(
    source_store: KnowledgeStore,
    change: str,
) -> None:
    parts = [post("direct", "1", seq=seq) for seq in (0, 1)]
    foreign = post("foreign", "2")
    await load(source_store, [*parts, foreign])
    current = SourceFilter(source_ids=("direct", "foreign"))
    failed = False

    def provider() -> SourceFilter:
        if failed:
            if change == "invalid":
                return cast(SourceFilter, None)
            raise RuntimeError("当前来源配置暂不可读取")
        return current

    retriever = Retriever(
        source_store,
        None,
        RetrievalSettings(max_df_ratio=1),
        source_filter=SourceFilter(source_ids=("direct",)),
        source_filter_provider=provider,
    )
    ids = [c.chunk_id for c in parts]
    assert await retriever.get([*ids, foreign.chunk_id]) == parts
    assert (await retriever.search("测试")).hits
    assert (await retriever.recent(NewsQuery(None, as_of="2026-05-22"))).chunks
    assert await retriever.page_text("测试帖子", chars=100)
    assert await retriever.search_pages("测试", ["测试帖子"], limit=2)
    assert (await retriever.fill_pages(ids[:1], room=100, min_hits=1))[1] == parts[1:]
    if change == "disabled":
        current = SourceFilter(source_ids=("direct", "foreign"), exclude_source_ids=("direct",))
    elif change == "removed":
        current = SourceFilter(source_ids=("foreign",))
    else:
        failed = True
    either = SourceFilter(
        any_of=(SourceFilter(source_ids=("direct",)), SourceFilter(source_platforms=("weibo",)))
    )
    assert await retriever.get([*ids, foreign.chunk_id], source_filter=either) == []
    assert (await retriever.search("测试", source_filter=either)).hits == []
    recent = await retriever.recent(NewsQuery(None, as_of="2026-05-22", source_filter=either))
    assert recent.chunks == [] and recent.matching_documents == 0
    assert await retriever.page_text("测试帖子", chars=100, source_filter=either) == ""
    assert await retriever.search_pages("测试", ["测试帖子"], limit=2, source_filter=either) == []
    plan, filled = await retriever.fill_pages(ids[:1], room=100, min_hits=1, source_filter=either)
    assert plan.missing == [] and plan.whole == {} and filled == []
    assert (await source_store.info()).chunks == 3


async def test_postgres_recent_scale_without_statistics_has_no_document_squared_join(
    source_store: KnowledgeStore,
) -> None:
    if not isinstance(source_store, PostgresKnowledgeStore):
        pytest.skip("3.2万片执行计划回归仅适用于显式本地TEST_DSN")
    # fixture独立UUID schema；新表不做ANALYZE，覆盖候选估成1行的最坏关联选择。
    await source_store._db.pool.execute("ALTER TABLE kb_chunks SET (autovacuum_enabled=false)")
    await source_store._db.pool.execute("""
        INSERT INTO kb_chunks(chunk_id, doc_key, type, title, text, date, seq, meta, row_hash, wiki)
        SELECT src || ':weibo:' || i || ':' || part, src || ':weibo:' || i,
               CASE WHEN src = 'huiji' THEN 'announcement' ELSE 'post' END,
               CASE WHEN i % 40 = 0 THEN '花合战维护情报' ELSE '日常公告' END,
               CASE WHEN i % 40 = 0 THEN '花合战近期资讯和活动说明' ELSE '不同日常内容' END,
               to_char(date '2026-11-24' - (i % 60), 'YYYY-MM-DD'), part,
               jsonb_build_object('source_platform', 'weibo',
                   'canonical_document_id', 'weibo:' || i,
                   'author_id', CASE WHEN i % 2 = 0 THEN 'alice' ELSE 'bob' END,
                   'published_at', to_char(date '2026-11-24' - (i % 60), 'YYYY-MM-DD')
                                   || 'T10:00:00+08:00'), i::text, src
        FROM generate_series(1, 8000) AS i, unnest(ARRAY['huiji', 'direct']) AS src,
             generate_series(0, 1) AS part
    """)
    queries = (
        (NewsQuery(("weibo",), as_of="2026-11-26"), 8000, 2),
        (NewsQuery(("weibo",), as_of="2026-11-26", keywords=("花合战",)), 200, 2),
        (
            NewsQuery(
                None,
                as_of="2026-11-26",
                source_filter=SourceFilter(
                    any_of=(
                        SourceFilter(source_ids=("direct",), author_ids=("alice",)),
                        SourceFilter(source_ids=("huiji",), author_ids=("bob",)),
                    )
                ),
            ),
            8000,
            1,
        ),
    )
    for query, matching, captures in queries:
        result = await asyncio.wait_for(source_store.recent_documents(query), timeout=5)
        assert result.matching_documents == matching and len(result.chunks) == 10
        assert result.source_latest_date == "2026-11-24" and result.undated_documents == 0
        assert len({canonical_document_id(c) for c in result.chunks}) == 10
        assert all(c.seq == 0 and len(provenance(c)["captures"]) == captures for c in result.chunks)
