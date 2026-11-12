"""核心带着的平台（``jianshanagent/platforms/``）：挂着的 wiki 在哪个平台上，就多出哪一份。

- 灰机：Data 命名空间的查询接口（查询、知识库各接一份）、灰机约定的清洗规则；
- BWiki：语义查询、取数放慢、平台组件的清洗规则。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from jianshanagent.core.config import AppConfig, load_config
from jianshanagent.core.sites import EXTENSION_KNOWLEDGE, EXTENSION_WIKI, load_sites
from jianshanagent.core.wiki import load_wiki
from jianshanagent.platforms.bwiki.wiki import BwikiPlatform, SemanticRow, _text
from jianshanagent.platforms.huiji.knowledge import LABEL_DATA, HuijiKnowledge
from jianshanagent.platforms.huiji.wiki import HuijiData
from jianshanagent.plugins.knowledge.changes import Change, ChangeContext, Plan, plan_changes
from jianshanagent.plugins.knowledge.settings import load_settings
from jianshanagent.plugins.wiki_query.client import WikiQueryClient

HUIJI = "https://game.huijiwiki.example"
BWIKI = "https://wiki.biligame.example/game"
WIKIS: dict[str, dict[str, Any]] = {
    "huiji": {"id": "huiji", "api_url": f"{HUIJI}/api.php", "platform": "huiji"},
    "bwiki": {
        "id": "bwiki",
        "api_url": f"{BWIKI}/api.php",
        "site_base": BWIKI,
        "article_path": "/$1",
        "platform": "bwiki",
    },
    "plain": {"id": "plain", "api_url": "https://wiki.example/api.php"},
}


def deployment(tmp_path: Path, *wikis: str, **files: dict[str, Any]) -> AppConfig:
    """一个部署：挂这几个 wiki；``files`` 是部署目录里别的配置（文件名里的点写成两个下划线）。"""
    directory = tmp_path / "config"
    directory.mkdir(exist_ok=True)
    content = {"core.wiki": {"wikis": [WIKIS[name] for name in wikis]}}
    content |= {name.replace("__", "."): values for name, values in files.items()}
    for name, values in content.items():
        (directory / f"{name}.yaml").write_text(yaml.safe_dump(values, allow_unicode=True), "utf-8")
    return load_config(directory, with_overrides=False)


def test_each_wiki_gets_what_its_platform_adds_and_nothing_from_the_others(tmp_path: Path) -> None:
    config = deployment(tmp_path, "huiji", "bwiki", "plain")
    assert config.platforms == ["huiji", "bwiki", "mediawiki"] and config.sites == []
    huiji, bwiki, plain = (load_settings(config, name) for name in ("huiji", "bwiki", "plain"))

    # 灰机：灰机约定的清洗规则；知识库那边接着 Data 命名空间
    assert {"kb-ignore", "huiji-tt-preload"} <= set(huiji.clean.remove_classes)
    assert huiji.sites.ids == ("huiji",)
    # BWiki：取数放慢，导航性的内容不进知识库，标签页是 Tabber 扩展的；知识库那边没接东西
    assert (bwiki.fetch.max_concurrency, bwiki.fetch.requests_per_second) == (1, 0.5)
    assert "navigation-not-searchable" in bwiki.clean.remove_classes
    assert (bwiki.clean.tabber_class, bwiki.clean.tabber_item_class) == ("tabber", "tabbertab")
    assert bwiki.clean.tabber_label_attribute == "title"
    assert len(bwiki.sites) == 0
    assert load_sites(config, EXTENSION_KNOWLEDGE, load_wiki(config, "bwiki")) == []
    # 各补各的，互不串；纯 MediaWiki 的两边都没有
    assert "navigation-not-searchable" not in huiji.clean.remove_classes
    assert "kb-ignore" not in bwiki.clean.remove_classes
    assert not {"kb-ignore", "navigation-not-searchable"} & set(plain.clean.remove_classes)
    assert plain.fetch.requests_per_second != 0.5 and len(plain.sites) == 0
    assert "toc" in plain.clean.remove_classes and "toc" in bwiki.clean.remove_classes  # 通用的都在
    assert load_sites(config, EXTENSION_WIKI, load_wiki(config, "plain")) == []

    # BWiki 的站，页面在站的目录下面
    wiki = load_wiki(config, "bwiki")
    assert wiki.page_url("大天狗") == f"{BWIKI}/%E5%A4%A7%E5%A4%A9%E7%8B%97"
    assert (wiki.page_url_prefix, wiki.link_prefix) == (f"{BWIKI}/", "/game/")


# ---------- 灰机 ----------


async def test_huiji_reads_the_data_namespace_through_the_query_interface(tmp_path: Path) -> None:
    config = deployment(tmp_path, "huiji")
    wiki = load_wiki(config, "huiji")
    (data,) = load_sites(config, EXTENSION_WIKI, wiki)
    assert isinstance(data, HuijiData)
    with pytest.raises(RuntimeError, match="还没有接到查询客户端上"):
        _ = data.client
    seen: list[dict[str, str]] = []

    def answer(request: httpx.Request) -> httpx.Response:
        assert str(request.url).startswith(f"{HUIJI}/api/rest_v1/namespace/data?")
        seen.append(dict(request.url.params))
        wanted = json.loads(request.url.params["filter"])
        hit = wanted["_id"] == "Data:Soul.json"
        found = [{"_id": "Data:Soul.json", "name": "针女"}] if hit else []
        return httpx.Response(200, json={"_embedded": found})

    data.bind(WikiQueryClient(wiki, transport=httpx.MockTransport(answer)))
    assert await data.doc("Data:Soul.json") == {"_id": "Data:Soul.json", "name": "针女"}
    assert await data.doc("Data:Nope.json") is None
    assert seen[0] == {"filter": '{"_id": "Data:Soul.json"}'}

    # 接口在站内的地址可以在这个部署里换
    moved = deployment(tmp_path, "huiji", platform__huiji={"data_path": "/api/data"})
    (elsewhere,) = load_sites(moved, EXTENSION_WIKI, load_wiki(moved, "huiji"))
    elsewhere.bind(WikiQueryClient(wiki))
    assert elsewhere.data_url == f"{HUIJI}/api/data"


def test_huiji_treats_a_change_to_a_data_document_as_shared_data_changing(tmp_path: Path) -> None:
    config = deployment(tmp_path, "huiji", platform__huiji={"data_namespace": 3600})
    settings = load_settings(config, "huiji")
    platform = settings.sites.require("huiji")
    assert isinstance(platform, HuijiKnowledge) and platform.data_namespace == 3600
    assert platform.data_url(settings.wiki) == f"{HUIJI}/api/rest_v1/namespace/data"

    classify = platform.change_classifier(ChangeContext(settings))
    assert classify is not None
    plan = Plan()
    page = Change(1, "2026-11-11T03:00:00Z", 0, "针女", "edit")
    assert classify(page, plan) is None and plan.shared_data_at == ""  # 条目页不归它管
    older = Change(2, "2026-11-11T03:36:12Z", 3600, "Data:Soul.json", "edit")
    newer = Change(3, "2026-11-11T04:00:00Z", 3600, "Data:Hero.json", "edit")
    for change in (newer, older):
        verdict = classify(change, plan)
        assert verdict is not None and verdict.label == LABEL_DATA and verdict.relevant
    assert plan.shared_data_at == "2026-11-11T04:00:00Z"  # 记最近的那一次
    # 走一遍通用的归类：Data 的改动记在公共数据上，由自动更新过后把渲染的页面重取一遍
    planned = plan_changes([older], settings, ChangeContext(settings))
    assert planned.shared_data_at == older.timestamp and planned.counts == {LABEL_DATA: 1}


# ---------- BWiki ----------

#: 形状照抄 wiki.biligame.com/yys 的 action=ask 的真实返回。内容做了删减，
#: 一反木绵的日期改成了只写到年的
SHIKIGAMI: dict[str, dict[str, Any]] = {
    "大天狗": {
        "printouts": {
            "稀有度": ["SSR"],
            "上线日期": [{"timestamp": "1473379200", "raw": "1/2016/9/9"}],
            "其他形态": ["[[少羽大天狗]]"],
        },
        "fulltext": "大天狗",
        "fullurl": f"{BWIKI}/%E5%A4%A7%E5%A4%A9%E7%8B%97",
        "namespace": 0,
        "exists": "1",
        "displaytitle": "",
    },
    "一目连": {
        "printouts": {"稀有度": ["SSR"], "上线日期": [], "其他形态": []},
        "fulltext": "一目连",
        "fullurl": f"{BWIKI}/%E4%B8%80%E7%9B%AE%E8%BF%9E",
        "namespace": 0,
        "exists": "1",
        "displaytitle": "",
    },
    "一反木绵": {
        "printouts": {
            "稀有度": ["SR"],
            "上线日期": [{"timestamp": "1514764800", "raw": "1/2018"}],
            "其他形态": [],
        },
        "fulltext": "一反木绵",
        "fullurl": f"{BWIKI}/%E4%B8%80%E5%8F%8D%E6%9C%A8%E7%BB%B5",
        "namespace": 0,
        "exists": "1",
        "displaytitle": "",
    },
}


class FakeBwiki:
    """假的 BWiki：只认 action=ask，照 limit / offset 翻页。"""

    def __init__(self) -> None:
        self.queries: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        assert str(request.url).startswith(f"{BWIKI}/api.php") and params["action"] == "ask"
        self.queries.append(params["query"])
        conditions, *options = params["query"].split("|")
        if conditions != "[[式神名::+]]":
            return httpx.Response(200, json={"query": {"results": []}})  # 没有结果时是空列表
        numbers = dict(o.split("=") for o in options if "=" in o)
        limit, offset = int(numbers["limit"]), int(numbers["offset"])
        titles = list(SHIKIGAMI)[offset : offset + limit]
        body: dict[str, Any] = {"query": {"results": {t: SHIKIGAMI[t] for t in titles}}}
        if offset + limit < len(SHIKIGAMI):
            body["query-continue-offset"] = offset + limit
        return httpx.Response(200, json=body)


def _bwiki(tmp_path: Path, fake: FakeBwiki, **ask: int) -> BwikiPlatform:
    config = deployment(tmp_path, "bwiki", **({"platform__bwiki": {"ask": ask}} if ask else {}))
    wiki = load_wiki(config, "bwiki")
    (platform,) = load_sites(config, EXTENSION_WIKI, wiki)
    assert isinstance(platform, BwikiPlatform)
    platform.bind(WikiQueryClient(wiki, transport=httpx.MockTransport(fake)))
    return platform


async def test_bwiki_ask_returns_pages_with_the_values_of_their_properties(tmp_path: Path) -> None:
    fake = FakeBwiki()
    rows = await _bwiki(tmp_path, fake).ask("[[式神名::+]]", ["稀有度", "上线日期", "其他形态"])
    assert fake.queries == ["[[式神名::+]]|?稀有度|?上线日期|?其他形态|limit=500|offset=0"]
    assert [row.title for row in rows] == ["大天狗", "一目连", "一反木绵"]
    tengu = rows[0]
    assert tengu == SemanticRow(
        "大天狗",
        f"{BWIKI}/%E5%A4%A7%E5%A4%A9%E7%8B%97",
        # 日期写成 年-月-日；文本类型的值照原样
        {"稀有度": ("SSR",), "上线日期": ("2016-09-09",), "其他形态": ("[[少羽大天狗]]",)},
    )
    assert (tengu.first("稀有度"), tengu.first("没有的属性", "—")) == ("SSR", "—")
    assert rows[1].values["上线日期"] == () and rows[1].first("上线日期") == ""
    assert rows[2].first("上线日期") == "2018"  # 只写到年的日期只给到年


async def test_bwiki_ask_keeps_paging_until_the_results_run_out(tmp_path: Path) -> None:
    fake = FakeBwiki()
    rows = await _bwiki(tmp_path, fake, page_size=2).ask("[[式神名::+]]", ["稀有度"])
    assert [row.title for row in rows] == ["大天狗", "一目连", "一反木绵"]
    assert [q.rpartition("?稀有度|")[2] for q in fake.queries] == [
        "limit=2|offset=0",
        "limit=2|offset=2",
    ]

    fake = FakeBwiki()  # 最多要这么多条：够了就不再翻
    rows = await _bwiki(tmp_path, fake, page_size=2, max_results=2).ask("[[式神名::+]]")
    assert [row.title for row in rows] == ["大天狗", "一目连"] and len(fake.queries) == 1

    assert await _bwiki(tmp_path, FakeBwiki()).ask("[[稀有度::没有这一档]]", ["稀有度"]) == []


def test_bwiki_values_of_other_types_become_text() -> None:
    # 阴阳师站的属性只有文本和日期两种；这两种是 Semantic MediaWiki 接口文档里的形状
    page = {"fulltext": "少羽大天狗", "fullurl": f"{BWIKI}/少羽大天狗", "namespace": 0}
    assert _text(page) == "少羽大天狗"  # 页面类型：标题
    assert _text({"value": 12.5, "unit": "米"}) == "12.5米"  # 数量类型：带单位
    assert _text(288) == "288"
