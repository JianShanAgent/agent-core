"""近期资讯按文档/发布日期筛选，普通相关性检索保持原样。"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator, Sequence
from dataclasses import FrozenInstanceError, asdict, replace
from datetime import date, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock
from urllib.parse import parse_qsl, urlencode, urlsplit
from uuid import uuid4

import asyncpg
import pytest

from jianshanagent.core.db import MIGRATIONS_DIR, Database
from jianshanagent.core.embedding import EmbeddingClient, EmbeddingProvider
from jianshanagent.plugins.knowledge.news import NewsQuery, NewsResult
from jianshanagent.plugins.knowledge.retrieval import MODE_VECTOR, RetrievalSettings, Retriever
from jianshanagent.plugins.knowledge.store import (
    KbChunk,
    KnowledgeStore,
    MemoryKnowledgeStore,
    PostgresKnowledgeStore,
)

AS_OF = "2026-05-22"
DIM = 4096
PROVIDER = EmbeddingProvider(
    name="news-test", base_url="http://unused.test/v1", model="m", dimensions=DIM
)
FP = PROVIDER.fingerprint


def chunk(
    doc: str,
    published: str | None = "2026-05-20",
    *,
    seq: int = 0,
    text: str = "正文",
    title: str = "公告",
    source: str = "wechat",
    wiki: str = "",
    kind: str = "announcement",
) -> KbChunk:
    prefix = f"{wiki}:" if wiki else ""
    return KbChunk(
        chunk_id=f"{prefix}{doc}:{seq}",
        doc_key=f"{prefix}{doc}",
        kind=kind,
        title=title,
        text=text,
        date=published,
        seq=seq,
        wiki=wiki,
        meta={"source_platform": source},
    )


def query(**kwargs: Any) -> NewsQuery:
    return NewsQuery(source_platforms=("wechat",), as_of=AS_OF, **kwargs)


async def load(store: KnowledgeStore, chunks: Sequence[KbChunk]) -> None:
    for wiki in sorted({c.wiki for c in chunks}):
        await store.sync([(c, None) for c in chunks if c.wiki == wiki], fingerprint=FP, wiki=wiki)


@pytest.fixture(params=["memory", "postgres"])
async def store(request: pytest.FixtureRequest) -> AsyncIterator[KnowledgeStore]:
    if request.param == "memory":
        yield MemoryKnowledgeStore()
        return
    dsn = os.environ.get("TEST_DSN") or os.environ.get("JIANSHANAGENT_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("没有独立 TEST_DSN，跳过 PostgreSQL 实测")
    if urlsplit(dsn).hostname not in (None, "localhost", "127.0.0.1", "::1"):
        pytest.skip("仅允许本地独立测试库，禁止外网/生产连接")
    schema = f"test_news_{uuid4().hex}"
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


async def test_new_post_beats_similar_old_post_but_default_search_is_unchanged(
    store: KnowledgeStore,
) -> None:
    old = chunk("old", "2024-05-20", text="高度相似的旧活动")
    new = chunk("new", "2026-05-21", text="最新活动")
    v_old = [1.0] + [0.0] * (DIM - 1)
    v_new = [0.0, 1.0] + [0.0] * (DIM - 2)
    await store.sync([(old, v_old), (new, v_new)], fingerprint=FP)
    embedder = cast(EmbeddingClient, SimpleNamespace(provider=PROVIDER, embed_one=AsyncMock()))
    cast(Any, embedder).embed_one.return_value = v_old
    retriever = Retriever(store, embedder, RetrievalSettings(top_k=1, keyword_slots=0))
    before = await retriever.search("unrelatedquery")
    assert before.mode == MODE_VECTOR
    assert [h.chunk.doc_key for h in before.hits] == ["old"]
    recent = await retriever.recent(query(limit=1))
    assert recent.chunks == [new]
    assert cast(Any, embedder).embed_one.await_count == 1
    assert (await retriever.search("unrelatedquery")).hits == before.hits
    assert cast(Any, embedder).embed_one.await_count == 1
    assert [h.chunk for h in await store.vector_search(v_old, limit=1)] == [old]


async def test_long_document_does_not_consume_other_document_slots(store: KnowledgeStore) -> None:
    long = [chunk("long", "2026-05-22", seq=i) for i in range(10)]
    following = chunk("next", "2026-05-21")
    await load(store, [*reversed(long), following])
    result = await store.recent_documents(query(limit=2))
    assert result.chunks == [long[0], following]
    assert result.matching_documents == 2
    limited = await store.recent_documents(query(limit=1))
    assert limited.chunks == [long[0]]
    assert limited.matching_documents == 2


@pytest.mark.parametrize("first_date", [None, "bad-date", "2026-02-30"])
async def test_representative_copies_document_date_without_changing_stored_chunks(
    store: KnowledgeStore,
    first_date: str | None,
) -> None:
    first = replace(
        chunk("target", first_date, text="首片正文", wiki="mounted"), date_inferred="2024-01"
    )
    last = chunk("target", "2026-05-21", seq=1, text="后片正文", wiki="mounted")
    await load(store, [last, first])
    before = await store.keyword_search(("首片",), limit=10, max_df_ratio=1.0)
    assert [hit.chunk for hit in before] == [first]
    result = await store.recent_documents(query())
    assert result == NewsResult([replace(first, date="2026-05-21")], "2026-05-21", 1, 0)
    assert result.chunks[0].doc_key == first.doc_key
    assert result.chunks[0].wiki == first.wiki
    assert result.chunks[0].meta == first.meta
    assert await store.get([first.chunk_id]) == [first]
    assert await store.keyword_search(("首片",), limit=10, max_df_ratio=1.0) == before


async def test_all_topic_words_may_match_different_chunks_or_title(store: KnowledgeStore) -> None:
    first = chunk("target", seq=0, text="版本前言", title="海国活动")
    last = chunk("target", seq=1, text="新增限定皮肤", title="海国活动")
    unrelated = chunk("unrelated", "2026-05-22", text="其他活动")
    one_word = chunk("partial", "2026-05-21", text="限定皮肤")
    await load(store, [last, unrelated, one_word, first])
    result = await store.recent_documents(query(keywords=("海国", "皮肤")))
    assert result.chunks == [first]
    assert result.matching_documents == 1
    assert result.source_latest_date == "2026-05-22"
    assert (await store.recent_documents(query(keywords=("不存在",)))).chunks == []


async def test_case_equivalent_topic_words_count_once_across_chunks_before_limit(
    store: KnowledgeStore,
) -> None:
    first = chunk("target", "2026-05-10", title="ALPHA", text="前言")
    rest = chunk("target", "2026-05-10", seq=1, text="BETA")
    older = chunk("older", "2026-05-09", text="alpha beta")
    partial = [chunk("partial", "2026-05-21", seq=i, text="alpha") for i in range(4)]
    unrelated = [chunk(f"unrelated-{i}", AS_OF, text="无关内容") for i in range(20)]
    await load(store, [*unrelated, *partial, older, rest, first])
    result = await store.recent_documents(
        query(keywords=("ALPHA", "alpha", "BETA", "beta"), limit=1)
    )
    assert result.chunks == [first]
    assert result.matching_documents == 2
    assert result.source_latest_date == AS_OF
    assert (await store.recent_documents(query(keywords=("ALPHA", "alpha"), limit=1))).chunks == [
        partial[0]
    ]


async def test_historical_month_is_filtered_before_limit_and_boundaries_are_inclusive(
    store: KnowledgeStore,
) -> None:
    start = chunk("start", "2025-03-01")
    end = chunk("end", "2025-03-31")
    newer = [chunk(f"new-{i}", f"2026-05-{i + 1:02d}") for i in range(15)]
    await load(store, [chunk("before", "2025-02-28"), start, end, *newer])
    result = await store.recent_documents(query(date_from="2025-03-01", date_to="2025-03-31"))
    assert result.chunks == [end, start]
    assert result.matching_documents == 2
    assert result.source_latest_date == "2026-05-15"
    one_day = await store.recent_documents(query(date_from="2025-03-01", date_to="2025-03-01"))
    assert one_day.chunks == [start]


async def test_future_and_unreliable_dates_do_not_become_recent(store: KnowledgeStore) -> None:
    today = chunk("today", AS_OF)
    undated = [
        chunk("missing", None),
        chunk("missing", None, seq=1),
        chunk("month", "2026-05"),
        chunk("invalid-day", "2026-02-30"),
        chunk("compact", "20260521"),
        chunk("timestamp", "2026-05-21T12:00:00"),
    ]
    undated[0] = replace(
        undated[0],
        date_inferred="2026-05",
        meta={"source_platform": "wechat", "updated_at": "2026-05-22", "date": AS_OF},
    )
    await load(store, [today, chunk("future", "2026-05-23"), *undated])
    result = await store.recent_documents(query())
    assert result.chunks == [today]
    assert result.source_latest_date == AS_OF
    assert result.matching_documents == 1
    assert result.undated_documents == 5
    future_range = await store.recent_documents(query(date_from="2026-05-23"))
    assert future_range.chunks == []
    assert future_range.source_latest_date == AS_OF
    assert future_range.undated_documents == 5


async def test_default_as_of_is_today(store: KnowledgeStore) -> None:
    today = date.today()
    current = chunk("current", today.isoformat())
    tomorrow = chunk("tomorrow", (today + timedelta(days=1)).isoformat())
    await load(store, [current, tomorrow])
    result = await store.recent_documents(NewsQuery(source_platforms=("wechat",)))
    assert result.chunks == [current]
    assert result.source_latest_date == today.isoformat()


async def test_empty_results_keep_source_coverage_without_inventing_freshness(
    store: KnowledgeStore,
) -> None:
    assert await store.recent_documents(query()) == NewsResult(chunks=[])
    await load(store, [chunk("old", "2024-01-01"), chunk("missing", None)])
    result = await store.recent_documents(query(date_from="2026-05-01"))
    assert result == NewsResult([], "2024-01-01", 0, 1)
    await load(store, [chunk("missing", None), chunk("future", "2099-01-01")])
    assert await store.recent_documents(query()) == NewsResult([], None, 0, 1)


async def test_sources_kinds_and_unmounted_wikis_are_isolated(store: KnowledgeStore) -> None:
    mounted = chunk("same", "2026-05-20", wiki="mounted")
    dashen = chunk("dashen", "2026-05-21", source="dashen", wiki="mounted")
    unmounted = chunk("same", AS_OF, wiki="unmounted")
    missing = chunk("missing", None, wiki="unmounted")
    entry = chunk("entry", AS_OF, kind="entry", wiki="mounted")
    unknown = replace(chunk("unknown", AS_OF, wiki="mounted"), meta={})
    await load(store, [mounted, dashen, unmounted, missing, entry, unknown])
    retriever = Retriever(store, None, exclude_wikis={"unmounted"})
    result = await retriever.recent(query())
    assert result == NewsResult([mounted], "2026-05-20", 1, 0)
    both = await retriever.recent(NewsQuery(("wechat", "dashen"), as_of=AS_OF))
    assert both == NewsResult([dashen, mounted], "2026-05-21", 2, 0)
    assert await retriever.recent(NewsQuery(("unknown",), as_of=AS_OF)) == NewsResult([])
    assert await retriever.recent(NewsQuery((), as_of=AS_OF)) == NewsResult([])


async def test_same_day_order_and_first_chunk_are_stable(store: KnowledgeStore) -> None:
    a = chunk("a", seq=0)
    z = chunk("z", seq=0)
    await load(store, [z, chunk("a", seq=4), a])
    assert (await store.recent_documents(query())).chunks == [a, z]
    assert (await store.recent_documents(query(limit=1))).chunks == [a]


async def test_same_day_uses_actual_publication_time_before_document_key(
    store: KnowledgeStore,
) -> None:
    early = replace(
        chunk("a"), meta={"source_platform": "wechat", "published_at": "2026-05-20T09:00:00+08:00"}
    )
    late = chunk("z")
    late_last = replace(
        chunk("z", seq=1),
        meta={"source_platform": "wechat", "published_at": "2026-05-20T20:00:00+08:00"},
    )
    next_day = chunk("next-day", "2026-05-21")
    await load(store, [early, late_last, late, next_day])
    result = await store.recent_documents(query())
    assert result.chunks == [next_day, late, early]
    assert (await store.recent_documents(query(date_to="2026-05-20", limit=1))).chunks == [late]


async def test_offsets_compare_actual_instants_and_equal_instants_keep_stable_order(
    store: KnowledgeStore,
) -> None:
    stamps = {
        "z": "2026-05-20T10:00:00Z",
        "a": "2026-05-20T18:00:00+08:00",
        "b": "2026-05-20T05:00:00-0500",
        "y": "2026-05-20T17:30:00+08:00",
        "x": "2026-05-20T11:00:00.000001+00:00",
    }
    parts = [
        replace(chunk(doc), meta={"source_platform": "wechat", "published_at": stamp})
        for doc, stamp in stamps.items()
    ]
    await load(store, parts)
    result = await store.recent_documents(query())
    assert [c.doc_key for c in result.chunks] == ["x", "a", "b", "z", "y"]
    assert result.source_latest_date == "2026-05-20"
    assert result.matching_documents == 5


async def test_missing_bad_or_naive_publication_time_falls_back_to_document_key(
    store: KnowledgeStore,
) -> None:
    invalid = [
        None,
        "now",
        "infinity",
        "2026-05-20T23:59:00",
        "2026-02-30T23:59:00Z",
        "2026-05-20T23:59:00+25:00",
        "2026-05-20T23:59:00+08:00'; DROP TABLE kb_chunks; --",
        20260520,
        {"at": "2026-05-20T23:59:00Z"},
        "2026-05-20T23:59:60Z",
        "2026-05-20T24:00:00Z",
        "2026-05-20T23:59:00.1234567Z",
    ]
    fallbacks = [
        replace(
            chunk(f"a-{i:02d}"),
            meta={
                "source_platform": "wechat",
                "published_at": stamp,
                "updated_at": "2026-05-20T23:59:59Z",
            },
        )
        for i, stamp in enumerate(invalid)
    ]
    valid = replace(
        chunk("z"), meta={"source_platform": "wechat", "published_at": "2026-05-20T01:00:00Z"}
    )
    await load(store, [*reversed(fallbacks), valid])
    result = await store.recent_documents(query(limit=100))
    assert result.chunks == [valid, *fallbacks]
    assert result.undated_documents == 0


async def test_keywords_and_sources_are_literal_not_sql_or_like_patterns(
    store: KnowledgeStore,
) -> None:
    literal = chunk("literal", text="%_\\'; DROP TABLE kb_chunks; --")
    plain = chunk("plain", AS_OF, text="普通文本")
    other_source = chunk("injected-source", source="wechat') OR TRUE --")
    await load(store, [literal, plain, other_source])
    assert (await store.recent_documents(query(keywords=("%_\\",)))).chunks == [literal]
    assert (await store.recent_documents(query(keywords=("'; DROP TABLE",)))).chunks == [literal]
    assert (await store.recent_documents(query(keywords=("' OR 1=1 --",)))).chunks == []
    exact = await store.recent_documents(NewsQuery(("wechat') OR TRUE --",), as_of=AS_OF))
    assert exact.chunks == [other_source]
    assert await store.recent_documents(NewsQuery(("%",), as_of=AS_OF)) == NewsResult([])
    assert len((await store.recent_documents(query())).chunks) == 2


async def test_keyword_search_unchanged_by_recent_query(store: KnowledgeStore) -> None:
    old = chunk("old", "2024-01-01", text="独有词")
    new = chunk("new", AS_OF, text="其他词")
    await load(store, [old, new])
    before = await store.keyword_search(("独有",), limit=10, max_df_ratio=1.0)
    assert [h.chunk for h in before] == [old]
    assert (await store.recent_documents(query(limit=1))).chunks == [new]
    assert await store.keyword_search(("独有",), limit=10, max_df_ratio=1.0) == before


@pytest.mark.parametrize("field", ["date_from", "date_to", "as_of"])
@pytest.mark.parametrize("value", ["2026-5-2", "20260203", "2026-02-30", "2026-05", "bad"])
def test_query_rejects_invalid_dates(field: str, value: str) -> None:
    with pytest.raises(ValueError):
        NewsQuery(("wechat",), **{field: value})


@pytest.mark.parametrize("limit", [0, -1, 101, 1.5, True, "10"])
def test_query_rejects_unbounded_or_noninteger_limits(limit: Any) -> None:
    with pytest.raises(ValueError):
        NewsQuery(("wechat",), limit=limit)


def test_query_is_frozen_and_rejects_reversed_dates_or_invalid_sequences() -> None:
    with pytest.raises(FrozenInstanceError):
        cast(Any, query()).limit = 3
    assert not hasattr(query(), "__dict__")
    for kwargs in ({"source_platforms": "wechat"}, {"keywords": ("",)}):
        with pytest.raises(ValueError):
            NewsQuery(**{"source_platforms": ("wechat",), **kwargs})
    with pytest.raises(ValueError):
        query(date_from="2026-05-22", date_to="2026-05-21")


async def test_postgres_forwards_only_parameters_and_skips_db_for_empty_sources() -> None:
    representative = chunk("safe", text="literal %_ keyword")
    row = asdict(representative)
    row["type"] = row.pop("kind")
    row["meta"] = json.dumps(row["meta"])
    row["entities"] = json.dumps(row["entities"])
    row.update(source_latest_date=representative.date, matching_documents=1, undated_documents=0)
    fetch = AsyncMock(return_value=[row])
    db = cast(Database, SimpleNamespace(pool=SimpleNamespace(fetch=fetch)))
    store = PostgresKnowledgeStore(db)
    result = await store.recent_documents(query(keywords=("%_",)), exclude_wikis=("gone'",))
    assert result.chunks == [representative]
    sql, sources, excluded, as_of, start, end, words, limit = fetch.await_args.args
    assert "$1::text[]" in sql and "$2::text[]" in sql and "LIMIT $7" in sql
    assert "s.published_date AS date" in sql
    assert "SELECT DISTINCT lower(word) AS word" in sql
    assert "HAVING count(DISTINCT q.word) = (SELECT count(*) FROM query_words)" in sql
    assert "d.doc_key IN (SELECT doc_key FROM topic_documents)" in sql
    assert "p.doc_key = d.doc_key" not in sql  # 防止逐篇重新扫描完整 candidates 的性能回退
    assert "wechat" not in sql and "gone'" not in sql and "%_" not in sql
    assert sources == ["wechat"] and excluded == ["gone'"]
    assert (as_of, start, end, words, limit) == (AS_OF, None, None, ["%_"], 10)
    assert await store.recent_documents(NewsQuery(())) == NewsResult([])
    assert fetch.await_count == 1
    fetch.return_value = [
        {
            "chunk_id": None,
            "source_latest_date": "2024-01-01",
            "matching_documents": 0,
            "undated_documents": 3,
        }
    ]
    assert await store.recent_documents(query()) == NewsResult([], "2024-01-01", 0, 3)
