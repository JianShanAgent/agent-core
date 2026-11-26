from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from jianshanagent.core.wiki import WikiSite
from jianshanagent.platforms.huiji.knowledge import DATA_KIND, HuijiKnowledge
from jianshanagent.plugins.knowledge.fetcher import DiskCache, RateLimiter, WikiFetcher
from jianshanagent.plugins.knowledge.settings import (
    ChunkSettings,
    CleanSettings,
    DateSettings,
    FetchSettings,
    ReportSettings,
    Settings,
    SourceSettings,
)
from jianshanagent.plugins.knowledge.sources import PageRef, WikiSource

Handler = Callable[[httpx.Request], httpx.Response | Awaitable[httpx.Response]]


@pytest.fixture
async def sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Callable[..., WikiSource]]:
    fetchers: list[WikiFetcher] = []
    # 不实际等待限速；所有请求仍必须通过真实 fetcher 的预算与限速入口。
    monkeypatch.setattr(RateLimiter, "wait", AsyncMock())

    def make(
        handler: Handler | None = None,
        *,
        wiki_id: str = "huiji",
        budget: int | None = None,
        give_up_after: int | None = None,
    ) -> WikiSource:
        wiki = WikiSite(
            id=wiki_id,
            api_url=f"https://{wiki_id}.example/api.php",
            site_base=f"https://{wiki_id}.example",
        )
        root = tmp_path / wiki_id
        settings = Settings(
            wiki=wiki,
            fetch=FetchSettings(max_retries=0),
            sources=SourceSettings(),
            clean=CleanSettings(),
            chunk=ChunkSettings(),
            report=ReportSettings(),
            dates=DateSettings(),
            output_dir=root,
            cache_dir=root / "cache",
        )
        fetcher = None
        if handler is not None:
            fetcher = WikiFetcher(
                wiki,
                settings.fetch,
                transport=httpx.MockTransport(handler),
                budget=budget,
                give_up_after=give_up_after,
            )
            fetchers.append(fetcher)
        return WikiSource(fetcher, DiskCache(settings.cache_dir), settings)

    yield make
    for fetcher in fetchers:
        await fetcher.aclose()


def refs(count: int) -> list[PageRef]:
    return [
        PageRef(i, f"扫地工/微博/2026/11/26/{i}.json", 1000 + i, namespace=3500)
        for i in range(1, count + 1)
    ]


def query_ids(request: httpx.Request) -> list[str]:
    return json.loads(request.url.params["filter"])["_id"]["$in"]


def echo(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={"_embedded": [{"_id": data_id, "text": data_id} for data_id in query_ids(request)]},
    )


async def test_empty_and_all_current_cache_need_no_requests(
    sources: Callable[..., WikiSource],
) -> None:
    def forbidden(request: httpx.Request) -> httpx.Response:
        pytest.fail(f"不应访问网络: {request.url}")

    source = sources(forbidden)
    wanted = refs(3)
    for ref in wanted:
        source.cache.put(DATA_KIND, ref.page_id, ref.rev_id, {"text": ref.title})
    huiji = HuijiKnowledge()
    assert await huiji.data_many(source, []) == []
    got = await huiji.data_many(source, wanted)
    assert [item.ref for item in got] == wanted
    assert [item.value for item in got] == [{"text": ref.title} for ref in wanted]
    assert not any(item.error for item in got)
    assert source.fetcher.stats.requests == 0
    assert source.fetcher.stats.cache_hits == 3


async def test_only_missing_requested_and_unordered_full_ids_are_matched(
    sources: Callable[..., WikiSource],
) -> None:
    wanted = [
        PageRef(1, "扫地工/微博/2026/11/26/same.json", 11),
        PageRef(2, "扫地工/微博/2026/11/25/same.json", 12),
        PageRef(3, "扫地工/微信/有 空格.json", 13),
    ]
    seen: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(query_ids(request))
        assert request.url.host == "huiji.example"
        return httpx.Response(
            200,
            json={
                "_embedded": [
                    {"_id": "其他站/微博/same.json", "text": "wrong"},
                    {"_id": wanted[2].data_id, "text": "third"},
                    {"_id": wanted[0].data_id, "text": "first"},
                ]
            },
        )

    source = sources(handler)
    source.cache.put(DATA_KIND, 2, 12, {"text": "cached"})
    got = await HuijiKnowledge().data_many(source, wanted)
    assert seen == [[wanted[0].data_id, wanted[2].data_id]]
    assert [item.ref for item in got] == wanted
    assert [item.value["text"] for item in got] == ["first", "cached", "third"]
    assert not any(item.error for item in got)
    for item in got:
        assert source.cache.get(DATA_KIND, item.ref.page_id, item.ref.rev_id) == item.value
    assert source.fetcher.stats.by_kind == {DATA_KIND: 1}
    assert source.fetcher.stats.cache_hits == 1


async def test_partial_missing_falls_back_without_deletion_or_individual_requests(
    sources: Callable[..., WikiSource],
) -> None:
    wanted = refs(3)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"_embedded": [{"_id": wanted[0].data_id, "text": "new"}]})

    source = sources(handler)
    old = {"text": "old"}
    source.cache.put(DATA_KIND, 2, 1, old)
    got = await HuijiKnowledge().data_many(source, wanted)
    assert got[0].value["text"] == "new"
    assert got[1].value == old and not got[1].error
    assert got[2].value is None and wanted[2].data_id in got[2].error
    assert [(item.title, item.kind) for item in source.stale] == [(wanted[1].title, DATA_KIND)]
    assert source.cache.get(DATA_KIND, 2, 1) == old
    assert source.cache.get(DATA_KIND, 2, wanted[1].rev_id) is None
    assert source.cache.get(DATA_KIND, 3, wanted[2].rev_id) is None
    assert source.fetcher.stats.requests == 1


@pytest.mark.parametrize("failure", ["http", "network"])
async def test_batch_request_failure_falls_back_and_continues_next_batch(
    sources: Callable[..., WikiSource], failure: str
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            if failure == "network":
                raise httpx.ConnectError("mock offline", request=request)
            return httpx.Response(503)
        return echo(request)

    source = sources(handler)
    wanted = refs(3)
    source.cache.put(DATA_KIND, 1, 1, {"text": "old"})
    got = await HuijiKnowledge().data_many(source, wanted, batch_size=2)
    assert got[0].value == {"text": "old"} and not got[0].error
    assert got[1].error and got[1].value is None
    assert got[2].value["_id"] == wanted[2].data_id and not got[2].error
    assert len(source.stale) == 1
    assert calls == source.fetcher.stats.requests == 2
    assert source.cache.get(DATA_KIND, 1, wanted[0].rev_id) is None


async def test_whole_batch_deadline_stops_slow_responses_and_preserves_old_cache(
    sources: Callable[..., WikiSource],
) -> None:
    calls = 0

    async def slow(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        # MockTransport 不执行 HTTP 读超时；整批期限必须独立终止等待。
        await asyncio.sleep(1)
        return echo(request)

    source = sources(slow)
    source.settings = replace(
        source.settings, fetch=replace(source.settings.fetch, timeout_seconds=0.01)
    )
    wanted = refs(3)
    source.cache.put(DATA_KIND, 1, 1, {"text": "old"})
    got = await HuijiKnowledge().data_many(source, wanted, batch_size=2)
    assert calls == 1  # 超时后不再把其余批次逐个拖到期限。
    assert got[0].value == {"text": "old"} and not got[0].error
    assert len(source.stale) == 1
    assert all("整批期限" in item.error for item in got[1:])
    assert source.cache.get(DATA_KIND, 1, wanted[0].rev_id) is None


async def test_offline_replays_only_current_revision(sources: Callable[..., WikiSource]) -> None:
    source = sources()
    wanted = refs(3)
    source.cache.put(DATA_KIND, 1, wanted[0].rev_id, {"text": "current"})
    source.cache.put(DATA_KIND, 2, 1, {"text": "old"})
    got = await HuijiKnowledge().data_many(source, wanted)
    assert got[0].value == {"text": "current"} and not got[0].error
    assert all(item.value is None and "离线模式" in item.error for item in got[1:])
    assert not source.stale
    assert source.cache.get(DATA_KIND, 2, 1) == {"text": "old"}


@pytest.mark.parametrize(
    ("batch_size", "count", "sizes"),
    [
        (50, 101, [25, 25, 25, 25, 1]),
        (500, 51, [25, 25, 1]),
        (2, 5, [2, 2, 1]),
        (0, 3, [1, 1, 1]),
        (-8, 2, [1, 1]),
    ],
)
async def test_batch_size_and_request_count_are_bounded(
    sources: Callable[..., WikiSource], batch_size: int, count: int, sizes: list[int]
) -> None:
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        batch = query_ids(request)
        seen.append(len(batch))
        assert len(batch) <= int(request.url.params["limit"]) <= 25
        assert len(str(request.url).encode()) <= 3000
        return echo(request)

    source = sources(handler)
    got = await HuijiKnowledge().data_many(source, refs(count), batch_size=batch_size)
    assert seen == sizes
    assert len(got) == count and not any(item.error for item in got)
    assert source.fetcher.stats.requests == len(sizes)
    assert source.fetcher._limiter.wait.await_count == len(sizes)


async def test_batches_are_serial_without_increasing_fetcher_concurrency(
    sources: Callable[..., WikiSource],
) -> None:
    active = peak = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0)
        active -= 1
        return echo(request)

    source = sources(handler)
    got = await HuijiKnowledge().data_many(source, refs(7), batch_size=2)
    assert not any(item.error for item in got)
    assert peak == 1
    assert source.fetcher.settings.max_concurrency == 2
    assert source.fetcher.stats.requests == 4


async def test_out_of_batch_document_is_never_cached_under_another_revision(
    sources: Callable[..., WikiSource],
) -> None:
    wanted = refs(2)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={"_embedded": [{"_id": wanted[1].data_id, "text": f"batch-{calls}"}]},
        )

    source = sources(handler)
    got = await HuijiKnowledge().data_many(source, wanted, batch_size=1)
    assert got[0].error and got[0].value is None
    assert source.cache.get(DATA_KIND, 1, wanted[0].rev_id) is None
    assert got[1].value["text"] == "batch-2"
    assert source.cache.get(DATA_KIND, 2, wanted[1].rev_id) == got[1].value
    assert calls == 2


async def test_long_chinese_paths_split_before_encoded_url_limit(
    sources: Callable[..., WikiSource],
) -> None:
    wanted = [PageRef(i, f"扫地工/微博/{'中文站点名称' * 8}/{i}.json", i) for i in range(1, 51)]
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert len(str(request.url).encode()) <= 3000
        return echo(request)

    source = sources(handler)
    got = await HuijiKnowledge().data_many(source, wanted)
    assert len(seen) > 1
    assert [data_id for request in seen for data_id in query_ids(request)] == [
        ref.data_id for ref in wanted
    ]
    assert not any(item.error for item in got)


async def test_single_oversized_id_does_not_send_request(
    sources: Callable[..., WikiSource],
) -> None:
    source = sources(echo)
    wanted = [PageRef(1, "过长" * 1000, 2), refs(1)[0]]
    got = await HuijiKnowledge().data_many(source, wanted)
    assert "地址过长" in got[0].error and got[0].value is None
    assert got[1].value["_id"] == wanted[1].data_id
    assert source.fetcher.stats.requests == 1


@pytest.mark.parametrize("guard", ["budget", "give_up"])
async def test_fetcher_request_guards_are_not_bypassed(
    sources: Callable[..., WikiSource], guard: str
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503) if guard == "give_up" else echo(request)

    source = sources(handler, **({"budget": 1} if guard == "budget" else {"give_up_after": 1}))
    wanted = refs(5)
    source.cache.put(DATA_KIND, 5, 1, {"text": "fallback"})
    got = await HuijiKnowledge().data_many(source, wanted, batch_size=2)
    assert calls == source.fetcher.stats.requests == 1
    assert got[4].value == {"text": "fallback"} and not got[4].error
    assert len(source.stale) == 1
    assert all(item.error for item in got[2:4])
    assert source.fetcher._limiter.wait.await_count == 1


@pytest.mark.parametrize("body", [[], {"_embedded": "bad"}, {"_embedded": {"_id": "bad"}}])
async def test_malformed_response_is_a_recoverable_batch_error(
    sources: Callable[..., WikiSource], body: Any
) -> None:
    source = sources(lambda request: httpx.Response(200, json=body))
    wanted = refs(2)
    source.cache.put(DATA_KIND, 1, 1, {"text": "fallback"})
    got = await HuijiKnowledge().data_many(source, wanted)
    assert got[0].value == {"text": "fallback"} and not got[0].error
    assert got[1].error and got[1].value is None
    assert len(source.stale) == 1
    assert source.fetcher.stats.requests == 1


async def test_invalid_and_duplicate_document_ids_cannot_poison_cache(
    sources: Callable[..., WikiSource],
) -> None:
    wanted = refs(2)
    docs = [
        None,
        "not a document",
        {"text": "no id"},
        {"_id": ["not a string"]},
        {"_id": "elsewhere/1.json", "text": "foreign"},
        {"_id": wanted[0].data_id, "text": "first"},
        {"_id": wanted[0].data_id, "text": "conflicting"},
        {"_id": wanted[1].data_id, "text": "correct"},
    ]
    source = sources(lambda request: httpx.Response(200, json={"_embedded": docs}))
    got = await HuijiKnowledge().data_many(source, wanted)
    assert "重复 _id" in got[0].error and got[0].value is None
    assert got[1].value["text"] == "correct" and not got[1].error
    assert source.cache.get(DATA_KIND, 1, wanted[0].rev_id) is None
    assert source.cache.get(DATA_KIND, 2, wanted[1].rev_id) == got[1].value


async def test_duplicate_refs_share_one_request_and_preserve_input_positions(
    sources: Callable[..., WikiSource],
) -> None:
    wanted = refs(2)
    seen: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(query_ids(request))
        return echo(request)

    source = sources(handler)
    got = await HuijiKnowledge().data_many(source, [wanted[0], wanted[1], wanted[0]], batch_size=1)
    assert seen == [[ref.data_id] for ref in wanted]
    assert [item.ref for item in got] == [wanted[0], wanted[1], wanted[0]]
    assert got[0].value == got[2].value and not any(item.error for item in got)


async def test_same_ids_in_different_sources_keep_wiki_and_cache_isolation(
    sources: Callable[..., WikiSource],
) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        return httpx.Response(
            200,
            json={"_embedded": [{"_id": query_ids(request)[0], "text": request.url.host}]},
        )

    a, b = sources(handler, wiki_id="a"), sources(handler, wiki_id="b")
    wanted = refs(1)
    huiji = HuijiKnowledge()
    first = await huiji.data_many(a, wanted)
    second = await huiji.data_many(b, wanted)
    cached = await huiji.data_many(a, wanted)
    assert seen == ["a.example", "b.example"]
    assert first[0].value == cached[0].value
    assert first[0].value["text"] == "a.example"
    assert second[0].value["text"] == "b.example"
