"""联网搜索的调用层：请求怎么发、响应怎么读、出错怎么办。接口换成替身。"""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from deployment import FULL_CONFIG_DIR
from jianshanagent.core.config import AppConfig, load_config
from jianshanagent.core.websearch import (
    WebSearchClient,
    WebSearchError,
    WebSearchSettings,
    parse_pages,
)

REPO = load_config(FULL_CONFIG_DIR)


def _page(**fields: Any) -> dict[str, Any]:
    return {
        "name": "永生之海攻略",
        "url": "https://n.test/1",
        "snippet": "一两句话",
        "summary": "很长的摘要",
        "siteName": "九游网",
        "datePublished": "2021-06-10T08:00:00+08:00",
        **fields,
    }


def _reply(*pages: dict[str, Any]) -> dict[str, Any]:
    return {"code": 200, "data": {"webPages": {"value": list(pages)}}}


def _client(handler: Any, **settings: Any) -> WebSearchClient:
    return WebSearchClient(
        WebSearchSettings(base_url="https://s.test/v1", api_key="k", **settings),
        transport=httpx.MockTransport(handler),
        retry_base_seconds=0,
    )


def test_settings_come_from_the_config_and_need_the_key() -> None:
    files = {"core.websearch": {**REPO.get("core.websearch"), "enabled": True}}
    settings = WebSearchSettings.from_config(
        AppConfig(root=Path("."), files=files, secrets={"gitee_ai": {"api_key": " k "}})
    )
    assert settings is not None and settings.api_key == "k"
    assert settings.base_url == "https://ai.gitee.com/v1"  # 和嵌入是同一家、同一个密钥
    assert (settings.count, settings.summary, settings.freshness) == (6, True, "noLimit")

    # 密钥没填、没打开、没有这份配置：都是「没有联网搜索」，不报错
    assert WebSearchSettings.from_config(AppConfig(root=Path("."), files=files, secrets={})) is None
    off = {"core.websearch": REPO.get("core.websearch")}  # 仓库里的配置现在关着
    assert off["core.websearch"]["enabled"] is False
    secrets = {"gitee_ai": {"api_key": "k"}}
    assert WebSearchSettings.from_config(AppConfig(Path("."), off, secrets)) is None
    assert WebSearchSettings.from_config(AppConfig(Path("."), {}, secrets)) is None

    bad = {"core.websearch": {**files["core.websearch"], "freshness": "lastWeek"}}
    with pytest.raises(WebSearchError, match="freshness"):
        WebSearchSettings.from_config(AppConfig(Path("."), bad, secrets))


async def test_search_posts_the_query_and_reads_the_pages() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_reply(_page()))

    client = _client(handler, count=4, freshness="oneYear")
    pages = await client.search("  阴阳师 永生之海 ")
    assert await client.search("  ") == []  # 没有搜索词不搜（不花钱）
    await client.aclose()

    assert len(seen) == 1 and str(seen[0].url) == "https://s.test/v1/web-search"
    assert seen[0].headers["Authorization"] == "Bearer k"
    assert json.loads(seen[0].content) == {
        "query": "阴阳师 永生之海",
        "count": 4,
        "summary": True,
        "freshness": "oneYear",
    }
    page = pages[0]
    assert (page.title, page.url, page.site, page.date) == (
        "永生之海攻略",
        "https://n.test/1",
        "九游网",
        "2021-06-10",
    )
    assert page.text == "很长的摘要"  # 有摘要用摘要


def test_pages_that_cannot_be_used_are_dropped() -> None:
    data = _reply(
        _page(
            summary="", siteName="无", datePublished=""
        ),  # 没摘要用那一两句话；网站名「无」当没有
        _page(url="https://n.test/1", summary="重复的链接"),
        _page(url="", summary="没有链接"),
        _page(url="https://n.test/2", summary="", snippet=""),  # 没有内容
        _page(url="https://n.test/3", summary="字" * 30, datePublished="去年"),
        "不是对象",
    )
    pages = parse_pages(data, page_chars=10)
    assert [(p.url, p.text, p.site, p.date) for p in pages] == [
        ("https://n.test/1", "一两句话", "", ""),
        ("https://n.test/3", "字" * 10 + "…", "九游网", ""),  # 太长的截断；认不出的日期不要
    ]
    assert parse_pages({"webPages": {"value": [_page()]}}, page_chars=10)[
        0
    ].url  # 没有 data 那一层也认
    assert parse_pages({"code": 200, "data": {}}, page_chars=10) == []


async def test_temporary_failures_are_retried_and_refusals_are_not() -> None:
    replies = [httpx.Response(503), httpx.Response(200, json=_reply(_page()))]
    calls: list[int] = []

    def flaky(_request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return replies.pop(0)

    client = _client(flaky)
    assert len(await client.search("x")) == 1 and len(calls) == 2
    await client.aclose()

    def refused(_request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(401, text="bad key")

    calls.clear()
    client = _client(refused)
    with pytest.raises(WebSearchError, match="401"):
        await client.search("x")
    assert len(calls) == 1  # 平台明说了不行：不重试
    await client.aclose()

    # HTTP 200 但里面说出错了（余额不足之类）：当成出错，不当成「没搜到」
    client = _client(lambda _r: httpx.Response(200, json={"code": 402, "message": "余额不足"}))
    with pytest.raises(WebSearchError, match="余额不足"):
        await client.search("x")
    await client.aclose()
