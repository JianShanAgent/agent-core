"""联网搜索的调用层：请求怎么发、响应怎么读、出错怎么办。接口换成替身。"""

import json
import re
from datetime import UTC
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
    parse_annotations,
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


def _client(handler: Any, provider: str = "gitee_ai", **settings: Any) -> WebSearchClient:
    return WebSearchClient(
        WebSearchSettings(provider=provider, base_url="https://s.test/v1", api_key="k", **settings),
        timezone=UTC,
        transport=httpx.MockTransport(handler),
        retry_base_seconds=0,
    )


def _cited(**fields: Any) -> dict[str, Any]:
    return {
        "type": "url_citation",
        "url": "https://n.test/1",
        "title": "永生之海攻略",
        "summary": "网页的摘要",
        "site_name": "九游网",
        "publish_time": "2026-11-10T08:00:00+08:00",
        "logo_url": "https://n.test/logo.png",
        **fields,
    }


def _answer(*annotations: Any, searches: int = 1) -> dict[str, Any]:
    return {
        "choices": [{"message": {"content": "搜到了攻略。", "annotations": list(annotations)}}],
        "usage": {
            "total_tokens": 900,
            "web_search_usage": {"tool_usage": searches, "page_usage": len(annotations)},
        },
    }


def test_settings_come_from_the_config_and_need_the_key() -> None:
    files = {"core.websearch": {**REPO.get("core.websearch"), "enabled": True}}
    # 仓库里选的是 MiMo：按量付费的地址，密钥单独一项——对话模型的密钥（llm）填了也不拿来用
    both = {"llm": {"api_key": "tp-chat"}, "mimo_search": {"api_key": " sk-k "}}
    settings = WebSearchSettings.from_config(AppConfig(Path("."), files, both))
    assert settings is not None and (settings.provider, settings.api_key) == ("mimo", "sk-k")
    assert settings.base_url == "https://api.xiaomimimo.com/v1"
    assert settings.base_url != REPO.get("core.llm")["base_url"].rstrip("/")
    assert settings.force_search is True and settings.model == "mimo-v2.6-flash"
    assert (settings.count, settings.max_keyword, settings.timeout_seconds) == (6, 2, 20.0)
    only_chat = {"llm": {"api_key": "tp-chat"}, "gitee_ai": {"api_key": "k"}}
    assert WebSearchSettings.from_config(AppConfig(Path("."), files, only_chat)) is None

    # 换成模力方舟：和嵌入是同一家、同一个密钥
    gitee = {"core.websearch": {**files["core.websearch"], "provider": "gitee_ai"}}
    secrets = {"gitee_ai": {"api_key": " k "}}
    settings = WebSearchSettings.from_config(AppConfig(Path("."), gitee, secrets))
    assert settings is not None and (settings.provider, settings.api_key) == ("gitee_ai", "k")
    assert settings.base_url == "https://ai.gitee.com/v1" and settings.timeout_seconds == 10.0
    assert (settings.count, settings.summary, settings.freshness) == (6, True, "noLimit")

    # 密钥没填、没打开、没有这份配置：都是「没有联网搜索」，不报错
    assert WebSearchSettings.from_config(AppConfig(root=Path("."), files=files, secrets={})) is None
    off = {"core.websearch": REPO.get("core.websearch")}  # 仓库里的配置现在关着
    assert off["core.websearch"]["enabled"] is False
    assert WebSearchSettings.from_config(AppConfig(Path("."), off, both)) is None
    assert WebSearchSettings.from_config(AppConfig(Path("."), {}, both)) is None

    providers = gitee["core.websearch"]["providers"]
    stale = {**providers, "gitee_ai": {**providers["gitee_ai"], "freshness": "lastWeek"}}
    for change, mentions in (
        ({"providers": stale}, "freshness"),
        ({"provider": "bing"}, "provider"),
        ({"provider": "mimo", "providers": {**providers, "mimo": {"secret": "x"}}}, "base_url"),
    ):
        bad = {"core.websearch": {**gitee["core.websearch"], **change}}
        with pytest.raises(WebSearchError, match=mentions):
            WebSearchSettings.from_config(AppConfig(Path("."), bad, secrets))


async def test_mimo_is_asked_to_search_every_time_and_its_citations_are_the_pages() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_answer(_cited(), _cited(url="https://n.test/2")))

    client = _client(handler, "mimo", model="mimo-v2.6-flash", count=4, max_keyword=3)
    pages = await client.search("  阴阳师 永生之海 ")
    assert await client.search("  ") == []  # 没有搜索词不搜（不花钱）
    await client.aclose()

    assert len(seen) == 1 and str(seen[0].url) == "https://s.test/v1/chat/completions"
    assert seen[0].headers["Authorization"] == "Bearer k"
    body = json.loads(seen[0].content)
    # 强制联网：不让模型自己判断要不要搜
    assert body["tools"] == [
        {"type": "web_search", "force_search": True, "max_keyword": 3, "limit": 4}
    ]
    assert body["model"] == "mimo-v2.6-flash" and body["thinking"] == {"type": "disabled"}
    assert body["stream"] is False and body["max_completion_tokens"] == 16  # 回答不用，给最少
    system, user = body["messages"]
    # 模型不知道今天是哪一天，要告诉它（搜「最新」时才不会按训练时的年份去搜）
    assert system["role"] == "system" and re.search(r"现在是 \d{4}-\d{2}-\d{2} ", system["content"])
    assert user == {"role": "user", "content": "阴阳师 永生之海"}
    assert [page.url for page in pages] == ["https://n.test/1", "https://n.test/2"]
    page = pages[0]
    assert (page.title, page.text, page.site, page.date) == (
        "永生之海攻略",
        "网页的摘要",  # 要的是网页，不是模型写的那句回答
        "九游网",
        "2026-11-10",
    )


def test_citations_that_cannot_be_used_are_dropped() -> None:
    data = _answer(
        _cited(site_name="", publish_time=None),
        _cited(summary="重复的链接"),
        _cited(url="https://n.test/2", summary=""),  # 没有内容
        _cited(url="https://n.test/3", summary="字" * 30),
        _cited(url="https://n.test/4", type="file_citation"),  # 不是网页
        "不是对象",
    )
    pages = parse_annotations(data, page_chars=10)
    assert [(p.url, p.text, p.site, p.date) for p in pages] == [
        ("https://n.test/1", "网页的摘要", "", ""),
        ("https://n.test/3", "字" * 10 + "…", "九游网", "2026-11-10"),
    ]
    # 模型没搜（没有 annotations）、回复是空的：都是「没搜到」
    assert parse_annotations({"choices": [{"message": {"content": "不知道"}}]}, page_chars=10) == []
    assert parse_annotations({"choices": []}, page_chars=10) == []


async def test_an_account_without_the_search_plugin_is_told_what_to_do() -> None:
    calls: list[int] = []

    def refused(_request: httpx.Request) -> httpx.Response:
        calls.append(1)
        message = "web search tool found in the request body, but webSearchEnabled is false"
        return httpx.Response(400, json={"error": {"message": message}})

    client = _client(refused, "mimo", model="m")
    with pytest.raises(WebSearchError, match="联网服务插件") as caught:
        await client.search("x")
    assert caught.value.status == 400 and len(calls) == 1  # 不重试：再试也是一样
    await client.aclose()

    # HTTP 200 但里面是报错：当成出错，不当成「没搜到」
    client = _client(lambda _r: httpx.Response(200, json={"error": {"message": "欠费"}}), "mimo")
    with pytest.raises(WebSearchError, match="欠费"):
        await client.search("x")
    await client.aclose()


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
