import json
import math
import os
from array import array
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from deployment import FULL_CONFIG_DIR
from jianshanagent.core.config import AppConfig, load_config
from jianshanagent.core.db import Database
from jianshanagent.core.embedding import EmbeddingClient, EmbeddingError, EmbeddingProvider
from jianshanagent.core.llm import tool_result_message, user_message
from jianshanagent.core.plugin import PluginContext
from jianshanagent.plugins.chat.plugin import shorten_tool_results
from jianshanagent.plugins.knowledge.embed import VectorStore, text_hash
from jianshanagent.plugins.knowledge.load import load_into
from jianshanagent.plugins.knowledge.plugin import KnowledgePlugin
from jianshanagent.plugins.knowledge.retrieval import (
    MODE_HYBRID,
    MODE_KEYWORD,
    MODE_NONE,
    Ranked,
    RetrievalSettings,
    Retriever,
    merge,
    plan_pages,
)
from jianshanagent.plugins.knowledge.store import (
    ChunkPlace,
    Hit,
    KbChunk,
    KnowledgeStore,
    MemoryKnowledgeStore,
    PostgresKnowledgeStore,
)
from jianshanagent.plugins.knowledge.terms import bm25_score, extract_terms, term_weights

DIM = 4096  # 表里的向量列是 4096 维，测试也用同样的维度
PROVIDER = EmbeddingProvider(
    name="t", base_url="http://embed.test/v1", model="m", dimensions=DIM, max_retries=0
)
FP = PROVIDER.fingerprint


def unit(*weights: tuple[int, float]) -> array:
    """只有指定的几维非零的单位向量。"""
    vector = array("f", bytes(4 * DIM))
    norm = math.sqrt(sum(w * w for _, w in weights))
    for axis, weight in weights:
        vector[axis] = weight / norm
    return vector


def chunk(chunk_id: str, text: str, **kwargs: object) -> KbChunk:
    values: dict[str, object] = {
        "doc_key": f"page:{chunk_id}",
        "kind": "entry",
        "title": text.split("\n")[0],
        "url": f"https://wiki.test/{chunk_id}",
    }
    values.update(kwargs)
    return KbChunk(chunk_id=chunk_id, text=text, **values)  # type: ignore[arg-type]


RECORDS = [
    (chunk("a", "大天狗\n\n大天狗的技能羽刃暴风造成四段伤害"), unit((0, 1.0))),
    (
        chunk("b", "茨木童子\n\n茨木童子的技能地狱之手", entities=[{"name": "茨木童子"}]),
        unit((1, 1.0)),
    ),
    (
        chunk(
            "c",
            "为崽而战 ｜ 发布于 2024-08-07\n\n为崽而战活动 8 月 7 日开启，奖励头像框",
            kind="announcement",
            date="2024-08-07",
            section_path=("活动时间",),
            meta={"post_type": 2},
        ),
        unit((2, 1.0)),
    ),
    (chunk("d", "协同斗技\n\n协同斗技每周五 19:00 开放，ver 1.7.52 改版"), None),
]


# ---------- 拆词 ----------


def test_extract_terms_bigrams_for_chinese_and_whole_tokens_for_ascii() -> None:
    terms = extract_terms("大天狗的ＳＰ皮肤 Ver 1.7.52，奖励1,000勾玉")
    assert {"大天", "天狗", "狗的", "皮肤", "sp", "ver", "1.7.52", "1000", "勾玉"} <= terms
    assert "大" not in terms and "大天狗" not in terms
    assert extract_terms("鬼") == {"鬼"}  # 单个汉字没法两两组合，原样保留
    assert extract_terms("！？ ……") == set()


def test_term_weights_prefers_rare_terms_and_drops_common_and_unknown() -> None:
    weights = term_weights(
        {"稀有", "常见", "到处", "没有"}, {"稀有": 1, "常见": 15, "到处": 30}, 100
    )
    assert set(weights) == {"稀有", "常见"}  # 「到处」超过两成的切片都有，「没有」库里没出现过
    assert weights["稀有"] > weights["常见"] > 0


def test_bm25_rewards_repeats_and_discounts_long_text() -> None:
    weights = {"天狗": 2.0, "技能": 1.0}
    once = bm25_score(weights, "大天狗的技能" + "啊" * 94, 100)
    twice = bm25_score(weights, "大天狗大天狗的技能" + "啊" * 91, 100)
    longer = bm25_score(weights, "大天狗的技能" + "啊" * 394, 100)
    assert twice > once > longer > 0
    assert twice < 2 * once  # 多出现几次加分有上限
    assert bm25_score(weights, "完全无关的一段话", 100) == 0
    assert bm25_score(weights, "大天狗", 0) > 0  # 平均字数未知时不做长度折算，不报错


# ---------- 存储：内存和 Postgres 行为一致 ----------


@pytest.fixture
async def postgres_store() -> AsyncIterator[PostgresKnowledgeStore]:
    dsn = os.environ.get("JIANSHANAGENT_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("未设置 JIANSHANAGENT_TEST_POSTGRES_DSN，跳过 Postgres 实测")
    db = Database(dsn)
    await db.connect()
    await db.migrate()
    store = PostgresKnowledgeStore(db)
    try:
        yield store
    finally:
        await store.sync([], fingerprint=FP)  # 测试库用完清空
        await db.close()


@pytest.fixture(params=["memory", "postgres"])
def store(request: pytest.FixtureRequest) -> KnowledgeStore:
    if request.param == "memory":
        return MemoryKnowledgeStore()
    return request.getfixturevalue("postgres_store")


async def test_sync_is_idempotent_and_follows_the_given_records(store: KnowledgeStore) -> None:
    first = await store.sync(RECORDS, fingerprint=FP)
    assert (first.total, first.upserted, first.unchanged, first.deleted) == (4, 4, 0, 0)
    assert first.with_vectors == 3 and first.terms > 20
    info = await store.info()
    assert (info.chunks, info.with_vectors, info.fingerprint) == (4, 3, FP)

    again = await store.sync(RECORDS, fingerprint=FP)
    assert (again.upserted, again.unchanged, again.deleted) == (0, 4, 0)

    # 改一片的文字、去掉一片、给原来没有向量的补上向量
    changed = [
        (chunk("a", "大天狗\n\n大天狗的技能改成了五段伤害"), unit((0, 1.0))),
        RECORDS[1],
        (RECORDS[3][0], unit((3, 1.0))),
    ]
    third = await store.sync(changed, fingerprint=FP)
    assert (third.total, third.upserted, third.unchanged, third.deleted) == (3, 2, 1, 1)
    assert (await store.info()).with_vectors == 3
    assert await store.keyword_search(extract_terms("为崽而战"), limit=5) == []
    hits = await store.keyword_search(extract_terms("五段伤害"), limit=5)
    assert [hit.chunk.chunk_id for hit in hits] == ["a"]

    # 换了一种向量：所有带向量的行都要重写
    other = await store.sync(changed, fingerprint="t:other:4096")
    assert (other.upserted, other.unchanged) == (3, 0)
    assert (await store.info()).fingerprint == "t:other:4096"


async def test_vector_search_orders_by_similarity_and_skips_rows_without_vectors(
    store: KnowledgeStore,
) -> None:
    await store.sync(RECORDS, fingerprint=FP)
    hits = await store.vector_search(unit((1, 0.9), (2, 0.4), (0, 0.1)), limit=10)
    assert [hit.chunk.chunk_id for hit in hits] == ["b", "c", "a"]  # d 没有向量
    assert hits[0].score == pytest.approx(0.9 / math.sqrt(0.9**2 + 0.4**2 + 0.1**2), abs=1e-5)
    assert [h.chunk.chunk_id for h in await store.vector_search(unit((2, 1.0)), limit=1)] == ["c"]

    # 取回来的切片和存进去的一模一样
    by_id = {hit.chunk.chunk_id: hit.chunk for hit in hits}
    assert by_id["c"] == RECORDS[2][0]
    assert by_id["b"].entities == [{"name": "茨木童子"}]


async def test_keyword_search_scores_by_rare_terms(store: KnowledgeStore) -> None:
    await store.sync(RECORDS, fingerprint=FP)
    # 「的技」「技能」a、b 都有，「天狗」只有 a 有
    hits = await store.keyword_search(extract_terms("大天狗的技能"), limit=10, max_df_ratio=1.0)
    assert [hit.chunk.chunk_id for hit in hits] == ["a", "b"]
    assert hits[0].score > hits[1].score > 0
    # 太常见的词不算：四片里有两片含「技能」，比例上限调到四成后只剩「天狗」这些词起作用
    hits = await store.keyword_search(extract_terms("大天狗的技能"), limit=10, max_df_ratio=0.4)
    assert [hit.chunk.chunk_id for hit in hits] == ["a"]
    # 字母数字整段匹配；没有向量的切片也查得到
    hits = await store.keyword_search(extract_terms("1.7.52 改了什么"), limit=10)
    assert [hit.chunk.chunk_id for hit in hits] == ["d"]
    assert await store.keyword_search(extract_terms("完全无关"), limit=10) == []
    assert await store.keyword_search(set(), limit=10) == []
    # 限定页面：只在这些标题的切片里找
    only = await store.keyword_search(
        extract_terms("大天狗的技能"), limit=10, max_df_ratio=1.0, titles=["茨木童子"]
    )
    assert [hit.chunk.chunk_id for hit in only] == ["b"]
    only = await store.vector_search(unit((0, 1.0)), limit=10, titles=["茨木童子", "没有"])
    assert [hit.chunk.chunk_id for hit in only] == ["b"]
    assert len(await store.keyword_search(extract_terms("技能"), limit=1, max_df_ratio=1.0)) == 1


async def test_keyword_search_breaks_ties_by_how_much_a_chunk_talks_about_it(
    store: KnowledgeStore,
) -> None:
    # 三片都含有「千姬」「配音」，只看「有没有这个词」分不出高下
    records = [
        (chunk("1", "活动公告\n\n" + "这次活动的奖励很多，" * 30 + "千姬的配音也来了"), None),
        (chunk("2", "千姬\n\n千姬的配音演员是某某，千姬的配音在某年更换过"), None),
        (chunk("3", "式神列表\n\n千姬、大天狗、茨木童子的配音都在这里" + "，等等" * 40), None),
        *((chunk(f"x{i}", f"无关的第 {i} 片\n\n别的内容"), None) for i in range(20)),
    ]
    await store.sync(records, fingerprint=FP)
    assert (await store.info()).average_chars == pytest.approx(
        sum(len(c.text) for c, _ in records) / len(records)
    )
    hits = await store.keyword_search(extract_terms("千姬的配音是谁"), limit=3)
    assert hits[0].chunk.chunk_id == "2"  # 篇幅短、反复提到的排最前
    assert {hit.chunk.chunk_id for hit in hits} == {"1", "2", "3"}
    assert hits[0].score > hits[1].score > hits[2].score > 0


# ---------- 融合与降级 ----------


def _ids(ranked: list[Ranked]) -> str:
    return "".join(r.chunk.chunk_id for r in ranked)


def test_merge_keeps_keyword_leaders_and_fills_the_rest_by_vector_rank() -> None:
    hits = {x: Hit(chunk(x, x), 1.0) for x in "abcdefxyz"}
    vector = [hits[x] for x in "abcdef"]

    # 关键词前 2 名（x、y）一定在，其余 3 个名额给向量前 3 名；向量找到的排前面
    ranked = merge(vector, [hits[x] for x in "xyz"], top_k=5, keyword_slots=2)
    assert _ids(ranked) == "abcxy"
    assert (ranked[0].vector_rank, ranked[0].keyword_rank) == (1, None)
    assert (ranked[3].vector_rank, ranked[3].keyword_rank) == (None, 1)

    # 两路找到的是同一片：不重复占名额，省下来的给向量的下一名
    ranked = merge(vector, [hits[x] for x in "bxz"], top_k=5, keyword_slots=2)
    assert _ids(ranked) == "abcdx"
    assert (ranked[1].vector_rank, ranked[1].keyword_rank) == (2, 1)
    # 关键词的第 1 名在向量里排得很靠后，照样保留，按它在向量里的名次放
    assert _ids(merge(vector, [hits[x] for x in "fa"], top_k=3, keyword_slots=2)) == "abf"

    assert _ids(merge(vector, [hits[x] for x in "xyz"], top_k=4, keyword_slots=0)) == "abcd"
    assert _ids(merge(vector, [], top_k=4, keyword_slots=2)) == "abcd"
    # 向量用不了：名额全给关键词
    assert _ids(merge([], [hits[x] for x in "xyz"], top_k=2, keyword_slots=1)) == "xy"
    assert merge([], [], top_k=5, keyword_slots=2) == []


def embedder(handler: httpx.MockTransport) -> EmbeddingClient:
    return EmbeddingClient(PROVIDER, transport=handler, retry_base_seconds=0)


def answering(vector: array) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["dimensions"] == DIM and len(body["input"]) == 1
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": list(vector)}]})

    return httpx.MockTransport(handle)


async def test_retriever_fuses_both_routes() -> None:
    store = MemoryKnowledgeStore()
    await store.sync(RECORDS, fingerprint=FP)
    retriever = Retriever(store, embedder(answering(unit((1, 1.0), (0, 0.5)))))
    result = await retriever.search(" 协同斗技什么时候开 ")
    top = await retriever.search("协同斗技", top_k=1)
    await retriever.aclose()
    assert result.mode == MODE_HYBRID and result.vector_skipped is None
    ids = [hit.chunk.chunk_id for hit in result.hits]
    assert set(ids) == {"a", "b", "c", "d"}  # d 只有关键词一路能找到，照样进结果
    assert ids[0] == "b" and ids[-1] == "d"  # 向量找到的在前，关键词补进来的在后
    assert next(h for h in result.hits if h.chunk.chunk_id == "d").vector_rank is None
    assert len(top.hits) == 1


async def test_retriever_falls_back_to_keywords_when_vectors_are_unusable() -> None:
    store = MemoryKnowledgeStore()
    await store.sync(RECORDS, fingerprint=FP)

    def broken(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="overloaded")

    failing = Retriever(store, embedder(httpx.MockTransport(broken)))
    result = await failing.search("为崽而战的奖励")
    assert result.mode == MODE_KEYWORD and "503" in (result.vector_skipped or "")
    assert result.hits[0].chunk.chunk_id == "c"

    # 没配嵌入接口
    result = await Retriever(store, None).search("为崽而战的奖励")
    assert result.mode == MODE_KEYWORD and result.hits[0].chunk.chunk_id == "c"

    # 库里的向量是另一种：不拿去比（比出来没有意义），也不去调接口
    def unexpected(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("指纹不一致时不该调嵌入接口")

    other = MemoryKnowledgeStore()
    await other.sync(RECORDS, fingerprint="old:model:4096")
    result = await Retriever(other, embedder(httpx.MockTransport(unexpected))).search("为崽而战")
    assert result.mode == MODE_KEYWORD and "old:model:4096" in (result.vector_skipped or "")

    empty = await Retriever(MemoryKnowledgeStore(), None).search("为崽而战")
    assert (empty.mode, empty.hits) == (MODE_NONE, [])
    assert (await Retriever(store, None).search("   ")).mode == MODE_NONE


async def test_search_pages_picks_the_closest_chunks_of_the_given_pages() -> None:
    store = MemoryKnowledgeStore()
    records = [
        *RECORDS,
        (chunk("e1", "协同斗技\n\n协同斗技的奖励是勾玉"), unit((5, 1.0))),
        (chunk("e2", "协同斗技\n\n协同斗技的规则很长"), unit((6, 1.0), (1, 0.2))),
    ]
    await store.sync(records, fingerprint=FP)
    calls = 0

    def counting(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": list(unit((1, 1.0)))}]})

    retriever = Retriever(store, embedder(httpx.MockTransport(counting)))
    await retriever.search("茨木童子的技能")
    pages = ["协同斗技", "库里没有的页面"]
    # 只在这些页面的切片里按向量挑（d 没有向量，轮不到）；同一个问题的向量不重复算
    found = await retriever.search_pages("茨木童子的技能", pages, limit=5)
    assert [c.chunk_id for c in found] == ["e2", "e1"] and calls == 1
    found = await retriever.search_pages("茨木童子的技能", pages, limit=1, exclude={"e2"})
    assert [c.chunk_id for c in found] == ["e1"]
    assert await retriever.search_pages("茨木童子的技能", [], limit=5) == []
    await retriever.aclose()

    # 向量用不了：在这些页面里按关键词挑
    fallback = Retriever(store, None, RetrievalSettings(max_df_ratio=1.0))
    found = await fallback.search_pages("协同斗技的奖励", pages, limit=5)
    assert found[0].chunk_id == "e1" and {c.chunk_id for c in found} == {"d", "e1", "e2"}


# ---------- 导入 ----------


async def test_load_reads_chunks_and_matching_vectors(tmp_path: Path) -> None:
    rows = [
        {
            "chunk_id": "a",
            "doc_key": "page:1",
            "type": "entry",
            "title": "大天狗",
            "seq": 0,
            "text": "大天狗\n\n羽刃暴风",
            "url": "u",
            "source": "wiki 条目",
            "section_path": [],
            "date": None,
            "date_inferred": None,
            "entities": [],
            "meta": {},
            "embed": True,
        },
        {
            "chunk_id": "b",
            "doc_key": "page:2",
            "type": "announcement",
            "title": "公告",
            "seq": 1,
            "text": "公告 ｜ 发布于 2024-08-07\n\n新文字",
            "url": "u",
            "source": "网易大神",
            "section_path": ["一", "二"],
            "date": "2024-08-07",
            "date_inferred": None,
            "entities": [{"name": "晴明"}],
            "meta": {"post_type": 2},
            "embed": True,
        },
        {
            "chunk_id": "c",
            "doc_key": "page:3",
            "type": "entry",
            "title": "导航",
            "seq": 0,
            "text": "导航\n\n不算向量",
            "embed": False,
        },
    ]
    chunks_path = tmp_path / "chunks.jsonl"
    chunks_path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8"
    )
    vectors = VectorStore(tmp_path / "vectors", PROVIDER)
    vectors.open()
    vectors.append(
        [
            ("a", text_hash(rows[0]["text"]), unit((0, 1.0))),  # type: ignore[arg-type]
            ("b", text_hash("公告\n\n旧文字"), unit((1, 1.0))),  # 文字改过了：旧向量不能用
            ("c", text_hash(rows[2]["text"]), unit((2, 1.0))),  # type: ignore[arg-type]
        ]
    )
    vectors.close()

    store = MemoryKnowledgeStore()
    report = await load_into(store, chunks_path, tmp_path / "vectors", PROVIDER)
    assert (report.total, report.with_vectors, report.upserted) == (3, 1, 3)
    hits = await store.vector_search(unit((0, 1.0)), limit=5)
    assert [hit.chunk.chunk_id for hit in hits] == ["a"]
    found = await store.keyword_search(extract_terms("新文字"), limit=5)
    assert found[0].chunk.section_path == ("一", "二")
    assert found[0].chunk.date == "2024-08-07" and found[0].chunk.meta == {"post_type": 2}

    # 还没算过向量（目录不存在）也能导入，只是都没有向量；不会顺手建出空目录
    bare = await load_into(MemoryKnowledgeStore(), chunks_path, tmp_path / "none", PROVIDER)
    assert (bare.total, bare.with_vectors) == (3, 0)
    assert not (tmp_path / "none").exists()


# ---------- 插件 ----------


def _config(retrieval: dict[str, object]) -> AppConfig:
    return AppConfig(
        root=Path("."), files={"plugin.knowledge": {"retrieval": retrieval}}, secrets={}
    )


def test_plugin_registers_nothing_without_database_or_when_disabled() -> None:
    ctx = PluginContext("knowledge", _config({"enabled": True}), {})
    KnowledgePlugin().register(ctx)
    assert "knowledge" not in ctx.services
    ctx = PluginContext("knowledge", _config({"enabled": False}), {"database": object()})
    KnowledgePlugin().register(ctx)
    assert "knowledge" not in ctx.services


async def test_plugin_registers_the_retriever_with_repo_config() -> None:
    repo = load_config(FULL_CONFIG_DIR)
    assert "knowledge" in repo.get("core.plugins")["enabled"]
    config = AppConfig(
        root=Path("."), files={"plugin.knowledge": repo.plugin("knowledge")}, secrets={}
    )
    ctx = PluginContext("knowledge", config, {"database": object()})
    KnowledgePlugin().register(ctx)
    retriever: Retriever = ctx.services["knowledge"]
    assert retriever.embedder is None  # 这份配置里没有 core.embedding：只用关键词
    assert retriever.settings == RetrievalSettings(
        top_k=10,
        keyword_slots=2,
        max_df_ratio=0.2,
        embed_timeout_seconds=8.0,
        embed_retries=1,
    )
    assert ctx.tools == {}  # 检索只当服务用（查证工作流的第一级），不直接给对话模型当工具
    await retriever.aclose()


def test_plugin_uses_the_configured_embedding_provider_with_short_timeout() -> None:
    repo = load_config(FULL_CONFIG_DIR)
    config = AppConfig(
        root=Path("."),
        files={
            "plugin.knowledge": repo.plugin("knowledge"),
            "core.embedding": repo.get("core.embedding"),
        },
        secrets={"gitee_ai": {"api_key": "k"}},
    )
    ctx = PluginContext("knowledge", config, {"database": object()})
    KnowledgePlugin().register(ctx)
    provider = ctx.services["knowledge"].embedder.provider
    assert provider.fingerprint == "gitee:Qwen3-VL-Embedding-8B:4096"  # 和向量文件里的一致
    assert (provider.timeout_seconds, provider.max_retries) == (8.0, 1)


# ---------- 对话历史里的工具结果 ----------


def test_shorten_tool_results_only_touches_long_tool_messages() -> None:
    history = [
        user_message("问题"),
        tool_result_message("1", "x" * 50),
        tool_result_message("2", "短结果"),
        {"role": "assistant", "content": "y" * 50},
    ]
    out = shorten_tool_results(history, 10)
    assert out[1]["content"].startswith("x" * 10 + "…") and len(out[1]["content"]) < 50
    assert out[1]["tool_call_id"] == "1"
    assert out[0] is history[0] and out[2] is history[2] and out[3] is history[3]
    assert history[1]["content"] == "x" * 50  # 原来的历史不动
    assert shorten_tool_results(history, 0) is history


# ---------- 把挑中的页面补齐 ----------


def place(chunk_id: str, doc: str, seq: int, chars: int, *path: str) -> ChunkPlace:
    return ChunkPlace(chunk_id, doc, tuple(path), seq, chars)


#: 一个不长的页面：6 星这一节切成了 3 片（最后一片带着自己的小标题），另有一片 5 星
BOSSES = [
    place("b0", "boss", 0, 60),
    place("b1", "boss", 1, 600, "6星"),
    place("b2", "boss", 2, 600, "6星"),
    place("b3", "boss", 3, 300, "6星", "雪御前"),
    place("b4", "boss", 4, 600, "5星"),
]
#: 一个很长的页面：技能一节 3 片，传记一节很长
LONG = [
    place("s1", "shiki", 0, 900, "技能"),
    place("s2", "shiki", 1, 900, "技能"),
    place("s3", "shiki", 2, 900, "技能"),
    *(place(f"t{n}", "shiki", 3 + n, 1000, "传记") for n in range(9)),
]
ALONE = [place("x", "note", 0, 400)]


def test_a_page_with_several_picked_chunks_is_filled_up_to_the_whole_page() -> None:
    plan = plan_pages([*BOSSES, *ALONE], ["b2", "x", "b1"], min_hits=2, max_chars=8000, room=5000)
    assert plan.missing == ["b0", "b3", "b4"]  # 整页都补上，带小标题的那一片也在
    assert {cid: block for cid, (block, _) in plan.whole.items()} == dict.fromkeys(
        ["b0", "b1", "b2", "b3", "b4"], "boss"
    ) | {"x": "note"}  # 只有一片的页面本来就是整页
    assert [plan.whole[cid][1] for cid in ("b0", "b1", "b2", "b3", "b4")] == [0, 1, 2, 3, 4]
    assert plan.partial == frozenset()

    # 只沾到一片的页面不补，那一片算节选
    once = plan_pages(BOSSES, ["b2"], min_hits=2, max_chars=8000, room=5000)
    assert once.missing == [] and once.partial == {"b2"} and once.whole == {}


def test_a_long_page_only_gets_the_picked_section_and_the_rest_stays_an_excerpt() -> None:
    plan = plan_pages(LONG, ["s3", "t4", "s1", "t0"], min_hits=2, max_chars=8000, room=5000)
    assert plan.missing == ["s2"]  # 整页 1.17 万字太长；技能这一节 2700 字，补成整节
    assert {cid: plan.whole[cid][1] for cid in plan.whole} == {"s1": 0, "s2": 1, "s3": 2}
    assert len({block for block, _ in plan.whole.values()}) == 1
    assert plan.partial == {"t0", "t4"}  # 传记一节 9000 字也太长：不补，标成节选

    # 一节里的每一片都挑中了，那就是整节，不算节选
    whole = plan_pages(LONG, ["s1", "s2", "s3", "t0"], min_hits=2, max_chars=8000, room=0)
    assert whole.missing == [] and whole.partial == {"t0"}
    assert set(whole.whole) == {"s1", "s2", "s3"}


def test_pages_are_filled_whole_or_not_at_all_within_the_room_left() -> None:
    other = [place(f"o{n}", "other", n, 500) for n in range(3)]
    picked = ["o0", "o1", "b1", "b2", "b4"]
    plan = plan_pages([*BOSSES, *other], picked, min_hits=2, max_chars=8000, room=400)
    # 挑中得多的页面先补：boss 页差 360 字，补得起；other 页差 500 字，剩下的 40 字放不下，整页不补
    assert plan.missing == ["b0", "b3"]
    assert plan.partial == {"o0", "o1"}
    assert plan_pages(BOSSES, ["b1", "b2"], min_hits=2, max_chars=8000, room=0).missing == []
    # 全量（挑中一片就补、不限长短）：很长的页面也给整页
    full = plan_pages(LONG, ["t4"], min_hits=1, max_chars=10**9, room=20000)
    assert len(full.missing) == 11 and full.partial == frozenset()
    # 页面超过上限时不补整页；这里 6 星一节 1500 字，在上限以内
    capped = plan_pages(BOSSES, ["b1", "b2"], min_hits=2, max_chars=2000, room=5000)
    assert capped.missing == ["b3"] and capped.partial == frozenset()


async def test_a_failing_embedding_api_is_left_alone_for_a_while() -> None:
    class Down:
        """嵌入接口出故障：每问一次都要等到超时才知道。"""

        provider = PROVIDER
        calls = 0
        ok = False

        async def embed_one(self, _text: str) -> Any:
            self.calls += 1
            if not self.ok:
                raise EmbeddingError("超时")
            return unit((0, 1.0))

        async def aclose(self) -> None:
            pass

    store = MemoryKnowledgeStore()
    await store.sync(RECORDS, fingerprint=FP)
    embedder = Down()
    retriever = Retriever(store, embedder)  # type: ignore[arg-type]
    # 一个问题要算好几次问句向量（全库找、在点到名的页面里找、在站内搜到的页面里找）：
    # 接口出故障时只白等第一次，接下来一阵子直接按关键词找
    first = await retriever.search("大天狗的技能")
    assert first.mode == MODE_KEYWORD and first.hits
    assert "嵌入接口出错" in (first.vector_skipped or "")
    again = await retriever.search("茨木童子的技能")
    pages = await retriever.search_pages("大天狗的技能", ["大天狗"], limit=3)
    assert embedder.calls == 1 and pages
    assert again.mode == MODE_KEYWORD and "先不用它" in (again.vector_skipped or "")
    # 过了那一阵再试：接口好了就又用上向量
    embedder.ok = True
    retriever._embed_off_until = 0.0
    assert (await retriever.search("大天狗的技能")).mode == MODE_HYBRID
    assert embedder.calls == 2


async def test_a_page_is_read_back_from_its_chunks_in_order(store: KnowledgeStore) -> None:
    wiki = "https://wiki.test/wiki/"

    def piece(chunk_id: str, text: str, seq: int, *path: str) -> tuple[KbChunk, None]:
        page = chunk(
            chunk_id,
            text,
            doc_key="page:1",
            title="超鬼王",
            seq=seq,
            section_path=path,
            url=f"{wiki}超鬼王",
        )
        return page, None

    records = [
        piece("b2", "超鬼王 > 6星\n\n### 吸血姬\n\n生命：2", 2, "6星"),
        piece("b0", "超鬼王\n\n2026 年超鬼王数据", 0),
        piece("b4", "超鬼王 > 5星\n\n### 言和\n\n生命：4", 4, "5星"),
        piece("b1", "超鬼王 > 6星\n\n### 蟹姬\n\n生命：1", 1, "6星"),
        piece("b3", "超鬼王 > 6星 > 雪御前\n\n生命：3", 3, "6星", "雪御前"),
        # 同名的公告（不在 wiki 上）、别的页面：都不算这一页
        (chunk("n1", "超鬼王\n\n公告正文", kind="announcement", url="https://ds.test/1"), None),
        (chunk("o1", "山风\n\n别的页面", url=f"{wiki}山风"), None),
    ]
    await store.sync(records, fingerprint=FP)
    assert [c.chunk_id for c in await store.page("超鬼王", url_prefix=wiki)] == [
        "b0",
        "b1",
        "b2",
        "b3",
        "b4",
    ]
    assert len(await store.page("超鬼王")) == 6  # 不限链接时同名的公告也在
    assert await store.page("没有这一页", url_prefix=wiki) == []

    retriever = Retriever(store, None)
    text = await retriever.page_text(" 超鬼王 ", chars=10_000, url_prefix=wiki)
    # 每片开头的标题路径去掉了；进到新的章节补一行小标题，同一章节的几片只补一次
    assert text.split("\n\n") == [
        "2026 年超鬼王数据",
        "## 6星",
        "### 蟹姬",
        "生命：1",
        "### 吸血姬",
        "生命：2",
        "### 雪御前",
        "生命：3",
        "## 5星",
        "### 言和",
        "生命：4",
    ]
    # 只要开头：够字数就不往下接（整片整片地接，所以会多出一点）
    short = await retriever.page_text("超鬼王", chars=15, url_prefix=wiki)
    assert short.split("\n\n") == ["2026 年超鬼王数据", "## 6星", "### 蟹姬", "生命：1"]
    assert await retriever.page_text("没有这一页", chars=100, url_prefix=wiki) == ""
    assert await retriever.page_text("超鬼王", chars=0, url_prefix=wiki) == ""


async def test_outline_and_get_follow_the_pages_of_the_given_chunks(store: KnowledgeStore) -> None:
    records = [
        (chunk("p2", "页\n\n第三片", doc_key="page:1", seq=2, section_path=("甲", "乙")), None),
        (chunk("p0", "页\n\n第一片", doc_key="page:1", seq=0), None),
        (chunk("p1", "页\n\n第二片长一点", doc_key="page:1", seq=1, section_path=("甲",)), None),
        (chunk("q0", "别的页\n\n内容", doc_key="page:2"), None),
        (chunk("r0", "没挑中的页\n\n内容", doc_key="page:3"), None),
    ]
    await store.sync(records, fingerprint=FP)
    places = await store.outline(["p1", "q0", "不存在"])
    assert [(p.chunk_id, p.doc_key, p.section_path, p.seq) for p in places] == [
        ("p0", "page:1", (), 0),
        ("p1", "page:1", ("甲",), 1),
        ("p2", "page:1", ("甲", "乙"), 2),
        ("q0", "page:2", (), 0),
    ]
    assert [p.chars for p in places] == [6, 9, 6, 7]
    got = await store.get(["p2", "q0", "不存在"])
    assert {c.chunk_id: c.text for c in got} == {"p2": "页\n\n第三片", "q0": "别的页\n\n内容"}
    assert await store.outline([]) == [] and await store.get([]) == []

    plan, chunks = await Retriever(store, None).fill_pages(["p0", "p2"], room=100)
    assert plan.missing == ["p1"] and [c.chunk_id for c in chunks] == ["p1"]
