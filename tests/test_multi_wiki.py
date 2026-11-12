"""一个部署挂好几个 wiki：配置各有各的一份，查询各查各的、标明出处，
知识库和图库的内容在一张表里、按 wiki 分着对齐。"""

from __future__ import annotations

import io
import os
import sys
from collections.abc import AsyncIterator
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import yaml
from PIL import Image

from jianshanagent.core.config import AppConfig, load_config, write_override
from jianshanagent.core.db import Database
from jianshanagent.core.messages import ChatScope, InboundMessage, OutboundMessage
from jianshanagent.core.plugin import PluginContext
from jianshanagent.core.wiki import load_wiki, load_wikis, wiki_labels
from jianshanagent.plugins.chat.plugin import citations_footer
from jianshanagent.plugins.knowledge.cli import main as kb_main
from jianshanagent.plugins.knowledge.image_store import (
    ImageStore,
    KbImage,
    MemoryImageStore,
    PostgresImageStore,
)
from jianshanagent.plugins.knowledge.images import ImageSearch, ImageSettings
from jianshanagent.plugins.knowledge.retrieval import RetrievalSettings, Retriever
from jianshanagent.plugins.knowledge.settings import is_primary, kb_root, load_settings
from jianshanagent.plugins.knowledge.store import (
    KbChunk,
    KnowledgeStore,
    MemoryKnowledgeStore,
    PostgresKnowledgeStore,
)
from jianshanagent.plugins.research.evidence import (
    KnowledgeSource,
    SiteSearchSource,
    image_evidence,
    render,
)
from jianshanagent.plugins.wiki_query.client import WikiQueryClient
from jianshanagent.plugins.wiki_query.hub import WikiQueryHub
from jianshanagent.plugins.wiki_query.plugin import WikiQueryPlugin
from jianshanagent.testing.sites import register_plugins, site_packages, write_site

HUIJI = "https://huiji.example"
BWIKI = "https://bwiki.example"
TWO_WIKIS: dict[str, Any] = {
    "name": "某游戏",
    "wikis": [
        {"id": "huiji", "label": "灰机 wiki", "api_url": f"{HUIJI}/api.php"},
        {
            "id": "bwiki",
            "label": "BWiki",
            "api_url": f"{BWIKI}/game/api.php",
            "site_base": f"{BWIKI}/game",
            "article_path": "/$1",
        },
    ],
}
LABELS = {"huiji": "灰机 wiki", "bwiki": "BWiki"}
FP = "test:4096"
OTHER_FP = "other:4096"
DIM = 4096


def unit(axis: int, weight: float = 1.0) -> list[float]:
    """第 ``axis`` 维是 ``weight``、其余靠第 0 维补成单位长度的向量。"""
    vector = [0.0] * DIM
    vector[axis] = weight
    if axis and weight < 1.0:
        vector[0] = (1.0 - weight * weight) ** 0.5
    return vector


# ---------- 配置：每个 wiki 一份 ----------


def write_deployment(tmp_path: Path, wiki: dict[str, Any]) -> Path:
    config_dir = tmp_path / "config"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "core.wiki.yaml").write_text(yaml.safe_dump(wiki, allow_unicode=True), "utf-8")
    return config_dir


def two_site_plugins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """两个平台上各有同一个题材的站：``huiji_game`` 和 ``bwiki_game``，各补各的配置。"""
    huiji = write_site(
        site_packages(tmp_path, monkeypatch, "jianshanagent_site_huiji"),
        manifest={"id": "huiji"},
        capabilities=("extend",),
        config={"extend": {"plugin.knowledge": {"clean": {"remove_classes": ["huiji-nav"]}}}},
    )
    write_site(
        site_packages(tmp_path, monkeypatch, "jianshanagent_site_huiji_game"),
        manifest={"id": "huiji_game"},
        parent="huiji",
        capabilities=("extend",),
        config={
            "extend": {
                "plugin.wiki_query": {"wiki": {"search_ranking": "popular"}},
                "plugin.knowledge": {"sync": {"interval_seconds": 600}},
            }
        },
    )
    write_site(
        site_packages(tmp_path, monkeypatch, "jianshanagent_site_bwiki"),
        manifest={"id": "bwiki"},
        capabilities=("extend",),
        config={
            "extend": {
                "plugin.knowledge": {
                    "clean": {"remove_classes": ["bwiki-ad"]},
                    "fetch": {"requests_per_second": 0.5},
                }
            }
        },
    )
    write_site(
        site_packages(tmp_path, monkeypatch, "jianshanagent_site_bwiki_game"),
        manifest={"id": "bwiki_game"},
        parent="bwiki",
    )
    assert huiji.name == "jianshanagent_site_huiji"
    register_plugins(
        monkeypatch,
        sites={
            name: f"jianshanagent_site_{name}"
            for name in ("huiji", "huiji_game", "bwiki", "bwiki_game")
        },
    )


def test_each_wiki_reads_the_shared_config_plus_its_own_site_plugins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    two_site_plugins(tmp_path, monkeypatch)
    wiki = {**TWO_WIKIS, "wikis": [dict(w) for w in TWO_WIKIS["wikis"]]}
    wiki["wikis"][0] |= {"site": "huiji_game", "auth_header": "X-authkey"}
    wiki["wikis"][1] |= {"site": "bwiki_game"}
    config_dir = write_deployment(tmp_path, wiki)
    (config_dir / "secrets.yaml").write_text(
        "wiki:\n  huiji:\n    authkey: k-huiji\n  bwiki:\n    cookie: c-bwiki\n", "utf-8"
    )
    overrides = tmp_path / "overrides"
    config = load_config(config_dir, overrides)

    assert [entry.id for entry in config.wikis] == ["huiji", "bwiki"]
    # 用到的站点插件：两个 wiki 各自的那一串，上级在前
    assert config.sites == ["huiji", "huiji_game", "bwiki", "bwiki_game"]
    assert [m.id for m in config.wiki("bwiki").chain] == ["bwiki", "bwiki_game"]
    with pytest.raises(ValueError, match="没有叫「nope」的 wiki（挂着的有：huiji、bwiki）"):
        config.for_wiki("nope")

    # 不指明哪个 wiki 时读到的是主 wiki（排第一的）那一份
    huiji, bwiki = config.for_wiki("huiji"), config.for_wiki("bwiki")
    assert config.plugin("knowledge") == huiji.plugin("knowledge")
    assert huiji.plugin("wiki_query")["wiki"]["search_ranking"] == "popular"
    assert bwiki.plugin("wiki_query")["wiki"].get("search_ranking") != "popular"
    # 各补各的，互不串：灰机的清洗规则不会用到 BWiki 的页面上
    assert "huiji-nav" in huiji.plugin("knowledge")["clean"]["remove_classes"]
    assert "bwiki-ad" not in huiji.plugin("knowledge")["clean"]["remove_classes"]
    assert "bwiki-ad" in bwiki.plugin("knowledge")["clean"]["remove_classes"]
    assert "huiji-nav" not in bwiki.plugin("knowledge")["clean"]["remove_classes"]
    assert bwiki.plugin("knowledge")["fetch"]["requests_per_second"] == 0.5
    assert huiji.plugin("knowledge")["sync"]["interval_seconds"] == 600
    # 和 wiki 无关的配置只有一份
    assert bwiki.get("core.llm") == config.get("core.llm") and bwiki.wikis == config.wikis

    # 怎么访问：密钥按 wiki 分着写
    sites = {site.id: site for site in load_wikis(config)}
    assert (sites["huiji"].auth_header, sites["huiji"].auth_key) == ("X-authkey", "k-huiji")
    assert (sites["bwiki"].auth_key, sites["bwiki"].cookie) == (None, "c-bwiki")
    assert sites["huiji"].page_url("山风") == f"{HUIJI}/wiki/%E5%B1%B1%E9%A3%8E"
    assert sites["bwiki"].page_url("山风") == f"{BWIKI}/game/%E5%B1%B1%E9%A3%8E"
    assert load_wiki(config, "bwiki").label == "BWiki" and wiki_labels(config) == LABELS

    # 管理后台里改的（覆写）对每个 wiki 都算数：单个的值直接换；名单只带上增删的那几项，
    # 各个 wiki 自己补的还在
    edited = config.plugin("knowledge")
    edited = {
        **edited,
        "retrieval": {**edited["retrieval"], "top_k": 11},
        "clean": {
            **edited["clean"],
            "remove_classes": [
                *(c for c in edited["clean"]["remove_classes"] if c != "huiji-nav"),
                "added-on-the-web",
            ],
        },
    }
    write_override(config, "plugin.knowledge", edited)
    config = load_config(config_dir, overrides)
    for view in (config, config.for_wiki("huiji"), config.for_wiki("bwiki")):
        assert view.plugin("knowledge")["retrieval"]["top_k"] == 11
        assert "added-on-the-web" in view.plugin("knowledge")["clean"]["remove_classes"]
    assert "huiji-nav" not in config.plugin("knowledge")["clean"]["remove_classes"]
    assert "bwiki-ad" in config.for_wiki("bwiki").plugin("knowledge")["clean"]["remove_classes"]
    assert config.for_wiki("bwiki").plugin("knowledge")["fetch"]["requests_per_second"] == 0.5


def test_a_wiki_list_that_cannot_work_is_explained(tmp_path: Path) -> None:
    def load(wiki: dict[str, Any]) -> AppConfig:
        config = load_config(write_deployment(tmp_path, wiki), with_overrides=False)
        load_wikis(config)
        return config

    # 以前只能挂一个 wiki 时的写法
    with pytest.raises(ValueError, match="wikis"):
        load({"api_url": f"{HUIJI}/api.php", "site": ""})
    with pytest.raises(ValueError, match="huiji"):
        load({"wikis": [{"id": "huiji", "api_url": "a"}, {"id": "huiji", "api_url": "b"}]})
    with pytest.raises(ValueError, match="id"):
        load({"wikis": [{"id": "灰机", "api_url": f"{HUIJI}/api.php"}]})
    with pytest.raises(ValueError, match="「huiji」 要写 api_url"):
        load({"wikis": [{"id": "huiji"}]})
    # 只挂一个 wiki：不用标出处
    assert wiki_labels(load({"wikis": [TWO_WIKIS["wikis"][0]]})) == {}


# ---------- /查：各个 wiki 各查各的，标明出处 ----------


class FakeWikis:
    """两个 wiki 的接口：各有哪些页面（标题 → 摘要）。``down`` 里的站一访问就 503。"""

    def __init__(self, pages: dict[str, dict[str, str]]):
        self.pages = pages
        self.down: set[str] = set()
        self.calls: list[tuple[str, str]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host.split(".")[0]
        params = request.url.params
        self.calls.append((host, params.get("srsearch") or params.get("titles") or ""))
        if host in self.down:
            return httpx.Response(503)
        pages = self.pages[host]
        if params.get("list") == "search":
            word = params["srsearch"].removeprefix('intitle:"').removesuffix('"')
            found = [{"title": title} for title in pages if word in title]
            return httpx.Response(200, json={"query": {"search": found}})
        title = params["titles"]
        if title not in pages:
            return httpx.Response(200, json={"query": {"pages": [{"missing": True}]}})
        return httpx.Response(
            200, json={"query": {"pages": [{"title": title, "extract": pages[title]}]}}
        )


class Bot:
    def __init__(self, ctx: PluginContext):
        self.ctx = ctx
        self.hub: WikiQueryHub = ctx.services["wiki_query"]

    async def send(self, text: str) -> OutboundMessage:
        name, _, args = text.lstrip("/").partition(" ")
        message = InboundMessage(
            message_id="m1", chat_scope=ChatScope.C2C, chat_id="u1", user_id="u1", content=text
        )
        handler = next(c.handler for c in self.ctx.commands if c.name == name)
        out = await handler(message, args)
        assert isinstance(out, OutboundMessage)
        return out

    async def tool(self, name: str, **args: Any) -> Any:
        return await self.ctx.tools[name].handler(args, SimpleNamespace())


def wiki_bot(monkeypatch: pytest.MonkeyPatch, fake: FakeWikis, wiki: dict[str, Any]) -> Bot:
    monkeypatch.setattr(
        sys.modules["jianshanagent.plugins.wiki_query.plugin"],
        "WikiQueryClient",
        partial(WikiQueryClient, transport=httpx.MockTransport(fake)),
    )
    repo = load_config(Path(__file__).resolve().parent / "config")
    files = {"plugin.wiki_query": repo.plugin("wiki_query"), "core.wiki": wiki}
    ctx = PluginContext("wiki_query", AppConfig(root=Path("."), files=files, secrets={}), {})
    WikiQueryPlugin().register(ctx)
    return Bot(ctx)


async def test_lookup_answers_from_every_wiki_and_says_which(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeWikis(
        {
            "huiji": {"山风": "灰机上的山风。", "山风传记": "传记。", "荒": "灰机上的荒。"},
            "bwiki": {"山风": "BWiki 上的山风。", "山兔": "BWiki 上的山兔。"},
        }
    )
    bot = wiki_bot(monkeypatch, fake, TWO_WIKIS)
    try:
        assert bot.hub.multiple and [c.wiki.id for c in bot.hub.clients] == ["huiji", "bwiki"]

        # 两个 wiki 都有这个条目：两边的都回，各标出处，主 wiki 的在前
        text = (await bot.send("/查 山风")).content
        assert text.index("出自 灰机 wiki") < text.index("灰机上的山风")
        assert text.index("灰机上的山风") < text.index("出自 BWiki") < text.index("BWiki 上的山风")
        assert f"{HUIJI}/wiki/" in text and f"{BWIKI}/game/" in text
        # 只有一个 wiki 有：只回那一个，也标出处
        text = (await bot.send("/查 荒")).content
        assert "出自 灰机 wiki" in text and "灰机上的荒" in text and "BWiki" not in text
        # 没有完全同名的：候选列出来，同名的只列一次
        listed = (await bot.send("/查 山")).content
        assert listed.count("山风传记") == 1 and "山兔" in listed and listed.count("山风") == 2
        assert "没有找到" in (await bot.send("/查 不存在的")).content

        # 给模型的工具：结果里带着出自哪个 wiki，取内容时可以指明
        hits = await bot.tool("wiki_search", query="山风")
        assert [(h["title"], h["wiki"]) for h in hits if h["exact"]] == [
            ("山风", "huiji"),
            ("山风", "bwiki"),
        ]
        page = await bot.tool("wiki_get", title="山风", wiki="bwiki")
        assert (page["wiki"], page["extract"]) == ("bwiki", "BWiki 上的山风。")
        assert (await bot.tool("wiki_get", title="山风"))["wiki"] == "huiji"  # 不指明：第一个有的
        assert (await bot.tool("wiki_get", title="山兔"))["wiki"] == "bwiki"
        get = bot.ctx.tools["wiki_get"]
        assert get.parameters["properties"]["wiki"]["enum"] == ["huiji", "bwiki"]

        # 站内全文搜索各搜各的；查名字时同一个条目并成一条
        assert await bot.hub.fulltext_titles("山风") == {
            "huiji": ["山风", "山风传记"],
            "bwiki": ["山风"],
        }
        assert [(m.title, m.wiki) for m in await bot.hub.lookup_name("山风")] == [("山风", "huiji")]

        # 一个 wiki 一时访问不了：另一个照答；全都访问不了才说访问不了
        fake.down = {"bwiki"}
        for client in bot.hub.clients:
            client.cache_ttl = 0
            client._cache._store.clear()
        text = (await bot.send("/查 山风")).content
        assert "灰机上的山风" in text and "BWiki 上的山风" not in text
        fake.down = {"huiji", "bwiki"}
        assert "暂时访问不了" in (await bot.send("/查 山风")).content
    finally:
        await bot.hub.aclose()


async def test_with_one_wiki_nothing_is_labelled(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeWikis({"huiji": {"山风": "灰机上的山风。"}})
    bot = wiki_bot(monkeypatch, fake, {"wikis": [TWO_WIKIS["wikis"][0]]})
    try:
        text = (await bot.send("/查 山风")).content
        assert "灰机上的山风" in text and "出自" not in text
        assert "wiki" not in (await bot.tool("wiki_search", query="山风"))[0]
        assert "wiki" not in bot.ctx.tools["wiki_get"].parameters["properties"]
    finally:
        await bot.hub.aclose()


# ---------- 知识库：一张表，按 wiki 分着对齐 ----------


def row(local_id: str, title: str, text: str, **more: Any) -> dict[str, Any]:
    """切片文件里的一行（id 是各个 wiki 自己编的，会重）。"""
    return {
        "chunk_id": local_id,
        "doc_key": f"page:{local_id}",
        "type": "entry",
        "title": title,
        "text": f"{title}\n\n{text}",
        **more,
    }


def of(wiki: str, *rows: tuple[dict[str, Any], list[float] | None]) -> list[Any]:
    return [(KbChunk.from_json(item, wiki), vector) for item, vector in rows]


@pytest.fixture
async def database() -> AsyncIterator[Database]:
    dsn = os.environ.get("JIANSHANAGENT_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("未设置 JIANSHANAGENT_TEST_POSTGRES_DSN，跳过 Postgres 实测")
    db = Database(dsn)
    await db.connect()
    await db.migrate()
    try:
        yield db
    finally:
        # 测试库用完清空
        await db.pool.execute("TRUNCATE kb_chunks, kb_images")
        await db.pool.execute("DELETE FROM kb_meta")
        await db.pool.execute("DELETE FROM kb_terms")
        await db.close()


@pytest.fixture(params=["memory", "postgres"])
def store(request: pytest.FixtureRequest) -> KnowledgeStore:
    if request.param == "memory":
        return MemoryKnowledgeStore()
    return PostgresKnowledgeStore(request.getfixturevalue("database"))


@pytest.fixture(params=["memory", "postgres"])
def image_store(request: pytest.FixtureRequest) -> ImageStore:
    if request.param == "memory":
        return MemoryImageStore()
    return PostgresImageStore(request.getfixturevalue("database"))


async def test_chunks_of_several_wikis_share_one_table(store: KnowledgeStore) -> None:
    # 两个 wiki 的切片文件里 id 一样（页面号各编各的）：进了库各是各的
    huiji = of(
        "huiji",
        (row("1", "山风", "灰机说山风是 SSR 式神", url=f"{HUIJI}/wiki/山风"), unit(1)),
        (row("2", "荒", "灰机说荒是 SP 式神"), unit(2)),
    )
    bwiki = of(
        "bwiki",
        (row("1", "山风", "BWiki 说山风的技能是风袭", url=f"{BWIKI}/game/山风"), unit(1, 0.9)),
        (row("2", "山兔", "BWiki 说山兔会跳舞"), unit(3)),
    )
    first = await store.sync(huiji, fingerprint=FP, wiki="huiji")
    second = await store.sync(bwiki, fingerprint=FP, wiki="bwiki")
    assert (first.upserted, second.upserted, second.deleted) == (2, 2, 0)
    info = await store.info()
    assert (info.chunks, info.with_vectors, info.fingerprint) == (4, 4, FP)
    assert {wiki: mine.chunks for wiki, mine in info.wikis.items()} == {"huiji": 2, "bwiki": 2}

    # 检索时一起找，每片记着出处；库里的 id 带着 wiki，文件里的 id 还在
    hits = await store.vector_search(unit(1), limit=2, fingerprint=FP)
    assert [(h.chunk.chunk_id, h.chunk.wiki, h.chunk.local_id) for h in hits] == [
        ("huiji:1", "huiji", "1"),
        ("bwiki:1", "bwiki", "1"),
    ]
    assert hits[1].chunk.doc_key == "bwiki:page:1" and hits[1].chunk.url == f"{BWIKI}/game/山风"
    # 只在一个 wiki 的某几页里找
    only = await store.vector_search(unit(1), limit=5, titles=["山风"], wiki="bwiki")
    assert [h.chunk.chunk_id for h in only] == ["bwiki:1"]
    words = await store.keyword_search(["山风"], limit=5, max_df_ratio=1.0, wiki="huiji")
    assert [h.chunk.chunk_id for h in words] == ["huiji:1"]
    both = await store.keyword_search(["山风"], limit=5, max_df_ratio=1.0)
    assert {h.chunk.chunk_id for h in both} == {"huiji:1", "bwiki:1"}

    # 再导入一个 wiki：只对齐它自己的，另一个 wiki 的不动
    again = await store.sync(bwiki[:1], fingerprint=FP, wiki="bwiki")
    assert (again.unchanged, again.upserted, again.deleted) == (1, 0, 1)
    info = await store.info()
    assert {wiki: mine.chunks for wiki, mine in info.wikis.items()} == {"huiji": 2, "bwiki": 1}

    # 两个 wiki 的向量不是同一批算的：只和指纹对得上的比
    await store.sync(bwiki, fingerprint=OTHER_FP, wiki="bwiki")
    info = await store.info()
    assert info.fingerprint is None and info.has_vectors_of(FP) and info.has_vectors_of(OTHER_FP)
    assert not info.has_vectors_of("别的")
    hits = await store.vector_search(unit(1), limit=5, fingerprint=FP)
    assert {h.chunk.wiki for h in hits} == {"huiji"}

    # 从配置里拿掉了的 wiki：它的内容删掉
    assert await store.keep_only(["huiji"]) == 2
    info = await store.info()
    assert list(info.wikis) == ["huiji"] and info.chunks == 2 and info.fingerprint == FP
    assert await store.keep_only(["huiji"]) == 0


async def test_content_loaded_before_wikis_were_told_apart_goes_to_the_first_wiki(
    store: KnowledgeStore,
) -> None:
    # 分 wiki 之前导入的：没标出处，id 就是文件里的
    old = [
        (KbChunk.from_json(row("1", "山风", "旧的山风")), unit(1)),
        (KbChunk.from_json(row("9", "已经删掉的页面", "旧内容")), unit(2)),
    ]
    await store.sync(old, fingerprint=FP)
    assert list((await store.info()).wikis) == [""]
    # 别的 wiki 导入、清理拿掉了的 wiki，都不碰它们：这之前照常能查
    await store.sync(
        of("bwiki", (row("1", "山风", "BWiki 的山风"), unit(1))), fingerprint=FP, wiki="bwiki"
    )
    assert await store.keep_only(["huiji", "bwiki"]) == 0
    assert (await store.info()).chunks == 3

    # 主 wiki 导入时把它们换掉
    report = await store.sync(
        of("huiji", (row("1", "山风", "旧的山风"), unit(1))),
        fingerprint=FP,
        wiki="huiji",
        replace_unlabelled=True,
    )
    assert (report.upserted, report.deleted) == (1, 2)
    info = await store.info()
    assert {wiki: mine.chunks for wiki, mine in info.wikis.items()} == {"huiji": 1, "bwiki": 1}
    hits = await store.vector_search(unit(1), limit=5, fingerprint=FP)
    assert sorted(h.chunk.chunk_id for h in hits) == ["bwiki:1", "huiji:1"]


async def test_the_database_upgrade_keeps_what_was_loaded(database: Database) -> None:
    # 升级前库里的样子：切片没有出处，概况记在 stats 这一行（0011 这一步把它改叫 stats:）
    store = PostgresKnowledgeStore(database)
    await store.sync([(KbChunk.from_json(row("1", "山风", "旧的山风")), unit(1))], fingerprint=FP)
    keys = await database.pool.fetch("SELECT key FROM kb_meta ORDER BY key")
    assert [r["key"] for r in keys] == ["stats:"]
    assert await database.pool.fetchval("SELECT wiki FROM kb_chunks") == ""
    # 升级后、主 wiki 重新导入之前：照常能查
    retriever = Retriever(store, None, RetrievalSettings(max_df_ratio=1.0))
    assert [h.chunk.title for h in (await retriever.search("山风")).hits] == ["山风"]


# ---------- 检索和取证：标明出处 ----------


async def two_wiki_retriever() -> Retriever:
    store = MemoryKnowledgeStore()
    await store.sync(
        of(
            "huiji",
            (row("1", "协同斗技", "灰机：协同斗技每周五开放", url=f"{HUIJI}/wiki/协同斗技"), None),
            (row("2", "大天狗", "灰机：大天狗的奖励"), None),
        ),
        fingerprint=FP,
        wiki="huiji",
    )
    await store.sync(
        of(
            "bwiki",
            (
                row("1", "协同斗技", "BWiki：协同斗技的奖励是勾玉", url=f"{BWIKI}/game/协同斗技"),
                None,
            ),
            (row("2", "斗技", "BWiki：斗技的奖励"), None),
        ),
        fingerprint=FP,
        wiki="bwiki",
    )
    return Retriever(store, None, RetrievalSettings(max_df_ratio=1.0))


async def test_evidence_says_which_wiki_it_came_from() -> None:
    retriever = await two_wiki_retriever()
    found = await KnowledgeSource(retriever, sources=LABELS).collect("协同斗技的奖励", "", set())
    by_key = {item.key: item for item in found}
    assert by_key["huiji:1"].source == "灰机 wiki" and by_key["bwiki:1"].source == "BWiki"
    listing = render([by_key["huiji:1"], by_key["bwiki:1"]])
    assert "【资料 1】知识库｜出自 灰机 wiki｜条目" in listing
    assert "【资料 2】知识库｜出自 BWiki｜条目" in listing
    # 回答后面的参考资料：同名的页面靠出处分得开
    footer = citations_footer([by_key["huiji:1"], by_key["bwiki:1"]], markdown=False)
    assert f"- 协同斗技（灰机 wiki） {HUIJI}/wiki/协同斗技" in footer
    assert f"- 协同斗技（BWiki） {BWIKI}/game/协同斗技" in footer
    marked = citations_footer([by_key["bwiki:1"]], markdown=True)
    assert f"- [协同斗技]({BWIKI}/game/协同斗技)（BWiki）" in marked

    # 只挂一个 wiki：不标
    plain = await KnowledgeSource(retriever).collect("协同斗技的奖励", "", set())
    assert {item.source for item in plain} == {""}
    assert "出自" not in render(plain) and "（" not in citations_footer(plain[:1], markdown=False)


async def test_site_search_looks_in_each_wikis_own_pages() -> None:
    retriever = await two_wiki_retriever()
    asked: list[tuple[str, int]] = []

    async def search(keywords: str, limit: int) -> dict[str, list[str]]:
        asked.append((keywords, limit))
        # 灰机的站内搜索找到「协同斗技」，BWiki 的找到「斗技」：各在各的页面里挑
        return {"huiji": ["协同斗技"], "bwiki": ["斗技", "库里没有的页面"]}

    source = SiteSearchSource(retriever, search, pages=3, chunks=5, sources=LABELS)
    found = await source.collect("斗技的奖励", "斗技 奖励", set())
    assert asked == [("斗技 奖励", 3)]
    # BWiki 上也有「协同斗技」这一页，但那边的站内搜索没搜到它：不拿
    assert [(item.key, item.source) for item in found] == [
        ("huiji:1", "灰机 wiki"),
        ("bwiki:2", "BWiki"),
    ]
    # 名额不够时各个 wiki 轮着给，不会全是排在前面的那个 wiki 的
    narrow = SiteSearchSource(retriever, search, pages=3, chunks=1, sources=LABELS)
    assert [item.key for item in await narrow.collect("斗技的奖励", "斗技", set())] == ["huiji:1"]


# ---------- 图库：一个 wiki 一个 wiki 地比 ----------


def picture() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(buffer, "PNG")
    return buffer.getvalue()


def image(name: str, label: str, wiki: str) -> KbImage:
    return KbImage(image_id=name, kind="式神立绘", label=label, subject=label).of(wiki)


class OneVector:
    """嵌入接口的替身：什么图都算成同一个向量。"""

    provider = SimpleNamespace(fingerprint=FP)

    def __init__(self, vector: list[float]):
        self.vector = vector

    async def embed_image(self, _jpeg: bytes) -> list[float]:
        return self.vector


async def test_images_of_several_wikis_are_compared_wiki_by_wiki(image_store: ImageStore) -> None:
    settings = ImageSettings()
    how = settings.preprocess
    # 两个 wiki 上都有这张立绘，文件名还一样；灰机另有一张很像的换色版
    await image_store.sync(
        [
            (image("Skin_1.png", "山风的初始立绘", "huiji"), unit(1, 0.8)),
            (image("Skin_2.png", "山风的换色立绘", "huiji"), unit(1, 0.78)),
        ],
        fingerprint=FP,
        preprocess=how,
        wiki="huiji",
    )
    await image_store.sync(
        [
            (image("Skin_1.png", "山风的立绘", "bwiki"), unit(1, 0.99)),
            (image("Other.png", "山兔的立绘", "bwiki"), unit(2)),
        ],
        fingerprint=FP,
        preprocess=how,
        wiki="bwiki",
    )
    info = await image_store.info()
    assert {wiki: mine.images for wiki, mine in info.wikis.items()} == {"huiji": 2, "bwiki": 2}
    found = await image_store.search(unit(1), limit=5, wiki="bwiki")
    assert [(m.image.image_id, m.image.wiki, m.image.local_id) for m in found][:1] == [
        ("bwiki:Skin_1.png", "bwiki", "Skin_1.png")
    ]

    # 「认没认准」各个 wiki 自己判断：灰机上前两名挤在一起，认不准；BWiki 上只有这一张像，认准了。
    # 有认准的就只给认准的
    search = ImageSearch(image_store, OneVector(unit(1)), settings)  # type: ignore[arg-type]
    hits = await search.search(picture())
    assert [(hit.image_id, hit.wiki, hit.sure) for hit in hits] == [
        ("bwiki:Skin_1.png", "bwiki", True)
    ]
    evidence = image_evidence(hits, origin="以图搜图", number=1, total=1, sources=LABELS)
    assert (evidence[0].source, evidence[0].key) == ("BWiki", "image:bwiki:Skin_1.png")

    # 拿掉 BWiki 以后只剩灰机那两张：都给，都只是「有些像」
    assert await image_store.keep_only(["huiji"]) == 2
    hits = await search.search(picture())
    assert [(hit.wiki, hit.sure) for hit in hits] == [("huiji", False), ("huiji", False)]

    # 一个 wiki 的图片是另一种嵌入接口算的：不和它比，别的 wiki 照常
    await image_store.sync(
        [(image("Skin_1.png", "山风的立绘", "bwiki"), unit(1))],
        fingerprint=OTHER_FP,
        preprocess=how,
        wiki="bwiki",
    )
    assert {hit.wiki for hit in await search.search(picture())} == {"huiji"}


# ---------- 知识库的文件：一个 wiki 一层目录 ----------


def kb_config(tmp_path: Path, **knowledge: Any) -> AppConfig:
    config_dir = write_deployment(tmp_path, TWO_WIKIS)
    if knowledge:
        (config_dir / "plugin.knowledge.yaml").write_text(yaml.safe_dump(knowledge), "utf-8")
    return load_config(config_dir, with_overrides=False)


def test_each_wiki_keeps_its_files_in_its_own_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("JIANSHANAGENT_KB_DIR", raising=False)
    config = kb_config(tmp_path)
    root = (tmp_path / "data" / "kb").resolve()
    assert kb_root(config) == root
    main, second = load_settings(config), load_settings(config, "bwiki")
    assert (main.wiki.id, main.output_dir, main.cache_dir) == (
        "huiji",
        root / "huiji",
        root / "huiji" / "cache",
    )
    assert (second.wiki.id, second.output_dir) == ("bwiki", root / "bwiki")
    assert is_primary(config, main) and not is_primary(config, second)
    with pytest.raises(ValueError, match="没有叫「nope」的 wiki"):
        load_settings(config, "nope")

    # 换了地方放（容器里挂进来的卷）
    monkeypatch.setenv("JIANSHANAGENT_KB_DIR", str(tmp_path / "volume"))
    assert load_settings(config, "bwiki").output_dir == (tmp_path / "volume" / "bwiki").resolve()
    monkeypatch.delenv("JIANSHANAGENT_KB_DIR")

    # 以前的写法（一个目录放一个 wiki 的文件）：说清楚该怎么改
    old = kb_config(tmp_path, paths={"output_dir": "data/kb", "cache_dir": "data/kb/cache"})
    with pytest.raises(ValueError, match="output_dir、cache_dir 不用了：改成 paths.root"):
        load_settings(old)

    # 命令行：不指明就挨个做每个 wiki；只针对一个 wiki 才说得通的参数要带 --wiki
    config_dir = str(tmp_path / "config")
    assert kb_main(["--config-dir", config_dir, "sync", "--status"]) == 2
    assert "改成 paths.root" in capsys.readouterr().err
    (tmp_path / "config" / "plugin.knowledge.yaml").unlink()
    assert kb_main(["--config-dir", config_dir, "sync", "--status"]) == 0
    said = capsys.readouterr().err
    assert said.index("── huiji ──") < said.index("── bwiki ──") and said.count("上次检查") == 2
    assert kb_main(["--config-dir", config_dir, "--wiki", "bwiki", "sync", "--status"]) == 0
    said = capsys.readouterr().err
    assert said.count("上次检查") == 1 and "──" not in said
    assert kb_main(["--config-dir", config_dir, "run", "--only", "page"]) == 2
    assert "要在子命令前面加 --wiki <id>" in capsys.readouterr().err
    assert kb_main(["--config-dir", config_dir, "--wiki", "nope", "sync", "--status"]) == 2
    assert "没有叫「nope」的 wiki" in capsys.readouterr().err
    # 一直跑的那个进程管全部 wiki
    assert kb_main(["--config-dir", config_dir, "--wiki", "bwiki", "sync", "--health"]) == 2
    assert kb_main(["--config-dir", config_dir, "sync", "--health"]) == 1  # 还没跑起来
