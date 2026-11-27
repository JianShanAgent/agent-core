"""Only synthetic MockTransport fixtures; never proof of a real upstream collection."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

from jianshanagent import weibo_client, weibo_collect
from jianshanagent.core.config import AppConfig
from jianshanagent.core.sources import SourceSetup, source_instance
from jianshanagent.platforms.weibo import create_source_adapter
from jianshanagent.plugins.knowledge.source_import import (
    MAX_FILE_BYTES,
    MAX_LINE_BYTES,
    parse_jsonl,
)
from jianshanagent.weibo_auth import WeiboCredentialStore
from jianshanagent.weibo_client import WeiboClient, WeiboCollectError, viewer_confirmed
from jianshanagent.weibo_collect import fetch_weibo

_UID = "5896401674"
_CST = timezone(timedelta(hours=8))
_NOW = datetime(2026, 11, 27, 12, tzinfo=_CST)
_COOKIE = "synthetic-secret-cookie"
_VIEWER = '<script>try{window.$CONFIG={"uid":"12345","user":{"id":12345}}}catch(e){}</script>'


def post(post_id: int = 100, *, days: int = 1, **extra: Any) -> dict[str, Any]:
    return {
        "id": post_id,
        "idstr": str(post_id),
        "mblogid": f"Ab{post_id}",
        "user": {"id": int(_UID), "screen_name": "测试作者"},
        "created_at": (_NOW - timedelta(days=days)).strftime("%a %b %d %H:%M:%S %z %Y"),
        "visible": {"type": 0},
        "text_raw": "#测试# 合成正文",
        **extra,
    }


class Fixture:
    def __init__(self) -> None:
        self.pages: dict[int, list[dict[str, Any]]] = {1: [post()]}
        self.details: dict[str, Any] = {}
        self.calls: list[httpx.Request] = []
        self.identity = _VIEWER
        self.status = 200
        self.timeline_data: Any = None
        self.transport: httpx.MockTransport | None = None
        self.clients: list[WeiboClient] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        assert request.method == "GET"
        assert request.url.scheme == "https" and request.url.host == "weibo.com"
        assert _COOKIE in request.headers["cookie"]
        if request.url.path == "/":
            return httpx.Response(200, text=self.identity)
        if self.status != 200:
            return httpx.Response(self.status, text="secret body must not escape")
        if request.url.path == "/ajax/statuses/mymblog":
            assert request.url.params["uid"] == _UID
            page = int(request.url.params["page"])
            data = self.timeline_data
            if data is None:
                data = {"list": self.pages.get(page, []), "since_id": str(page * 1000)}
            return httpx.Response(200, json={"ok": 1, "data": data})
        assert request.url.path == "/ajax/statuses/longtext"
        detail = self.details.get(request.url.params["id"], {"longTextContent_raw": "完整合成正文"})
        return httpx.Response(200, json={"ok": 1, "data": detail})

    def client(self, cookies: list[dict[str, Any]]) -> WeiboClient:
        assert self.transport is not None
        result = WeiboClient(cookies, transport=self.transport)
        self.clients.append(result)
        return result


@pytest.fixture
def fixture(monkeypatch: pytest.MonkeyPatch) -> Fixture:
    result = Fixture()
    result.transport = httpx.MockTransport(result)
    monkeypatch.setattr(weibo_collect, "WeiboClient", result.client)
    monkeypatch.setattr(weibo_collect, "_now", lambda: _NOW)
    monkeypatch.setattr(weibo_client, "_MIN_INTERVAL", 0)
    monkeypatch.setattr(weibo_client, "_LAST_REQUEST", 0)
    monkeypatch.setattr(weibo_client, "_GATE", asyncio.Lock())
    return result


@pytest.fixture
def store(tmp_path: Path) -> WeiboCredentialStore:
    result = WeiboCredentialStore(tmp_path / "config")
    result.save([{"name": "SUB", "value": _COOKIE, "domain": ".weibo.com", "path": "/"}])
    return result


def normalize(tmp_path: Path, rows: list[dict[str, Any]]) -> Any:
    config = AppConfig(
        tmp_path / "config",
        {
            "core.wiki": {"wikis": []},
            "core.sources": {
                "sources": [
                    {"id": "social", "label": "微博", "platform": "weibo", "role": "official"},
                ]
            },
        },
        {},
    )
    adapter = create_source_adapter(SourceSetup(config, source_instance(config, "social")))
    return adapter.normalize(rows)


async def test_standard_rows_stable_and_compatible(fixture, store, tmp_path):
    progress = []
    first = await fetch_weibo({"uid": _UID}, store, progress=progress.append)
    second = await fetch_weibo({"uid": _UID}, store, checkpoint=first.checkpoint)
    assert first.complete and not first.limited and first.pages == 2
    assert first.rows == second.rows
    assert "collected_at" not in first.rows[0]
    assert first.checkpoint["initial_complete"] is True
    assert first.checkpoint["window_start"] == (_NOW - timedelta(days=30)).isoformat()
    assert second.checkpoint["window_start"] == (_NOW - timedelta(days=7)).isoformat()
    doc = normalize(tmp_path, first.rows)[0]
    assert doc.date == "2026-11-26"
    assert doc.meta["author_id"] == _UID
    assert doc.url == f"https://weibo.com/{_UID}/Ab100"
    assert progress[0]["accepted"] == 1
    assert all(client._client.is_closed for client in fixture.clients)


async def test_incremental_catches_17_day_outage_from_last_success(fixture, store, monkeypatch):
    previous_end = _NOW - timedelta(days=17)
    monkeypatch.setattr(weibo_collect, "_now", lambda: previous_end)
    fixture.pages = {}
    first = await fetch_weibo({"uid": _UID}, store)
    saved = copy.deepcopy(first.checkpoint)
    monkeypatch.setattr(weibo_collect, "_now", lambda: _NOW)
    fixture.pages = {1: [post(100 + i, days=days) for i, days in enumerate([1, 10, 16, 22, 25])]}
    batch = await fetch_weibo({"uid": _UID}, store, checkpoint=saved)
    assert batch.complete and {row["id"] for row in batch.rows} == {"100", "101", "102", "103"}
    assert batch.checkpoint["window_start"] == (previous_end - timedelta(days=7)).isoformat()
    assert batch.checkpoint["window_end"] == _NOW.isoformat()
    assert saved == first.checkpoint


async def test_failed_outage_retry_does_not_advance_success_watermark(fixture, store, monkeypatch):
    previous_end = _NOW - timedelta(days=17)
    monkeypatch.setattr(weibo_collect, "_now", lambda: previous_end)
    fixture.pages = {}
    first = await fetch_weibo({"uid": _UID}, store)
    saved = copy.deepcopy(first.checkpoint)
    monkeypatch.setattr(weibo_collect, "_now", lambda: _NOW)
    fixture.status = 403
    with pytest.raises(WeiboCollectError):
        await fetch_weibo({"uid": _UID}, store, checkpoint=saved)
    assert saved == first.checkpoint
    fixture.status = 200
    fixture.pages = {1: [post(101, days=15)]}
    retried = await fetch_weibo({"uid": _UID}, store, checkpoint=saved)
    assert retried.rows[0]["id"] == "101" and retried.complete
    assert retried.checkpoint["window_start"] == (previous_end - timedelta(days=7)).isoformat()


async def test_800_day_outage_segments_without_gaps(fixture, store, monkeypatch):
    previous_end = _NOW - timedelta(days=800)
    monkeypatch.setattr(weibo_collect, "_now", lambda: previous_end)
    fixture.pages = {}
    first = await fetch_weibo({"uid": _UID}, store)
    monkeypatch.setattr(weibo_collect, "_now", lambda: _NOW)
    fixture.pages = {
        1: [post(103, days=1), post(102, days=400), post(101, days=790), post(99, days=810)]
    }
    checkpoint = first.checkpoint
    collected = []
    completed_end = previous_end
    windows = []
    for _ in range(4):
        pending = weibo_collect._checkpoint({"uid": _UID, "overlap_days": 7}, checkpoint)
        start = datetime.fromisoformat(pending["window_start"])
        end = datetime.fromisoformat(pending["window_end"])
        assert start == completed_end - timedelta(days=7)
        assert timedelta(0) < end - start <= timedelta(days=365)
        windows.append((start, end))
        batch = await fetch_weibo({"uid": _UID}, store, checkpoint=checkpoint)
        collected.extend(row["id"] for row in batch.rows)
        completed_end = end
        checkpoint = batch.checkpoint
        if batch.complete:
            break
        assert batch.limited and not checkpoint["complete"]
        assert datetime.fromisoformat(checkpoint["window_start"]) == end - timedelta(days=7)
        assert checkpoint["next_page"] == 1 and not checkpoint["page_done"]
    else:
        pytest.fail("long outage did not converge through bounded windows")
    assert len(windows) == 3 and completed_end == _NOW
    assert set(collected) == {"101", "102", "103"}


async def test_pending_window_after_another_long_outage_remains_resumable(
    fixture, store, monkeypatch
):
    previous_end = _NOW - timedelta(days=800)
    monkeypatch.setattr(weibo_collect, "_now", lambda: previous_end)
    fixture.pages = {}
    first = await fetch_weibo({"uid": _UID}, store)
    monkeypatch.setattr(weibo_collect, "_now", lambda: previous_end + timedelta(days=20))
    fixture.pages = {1: [post(i, days=790) for i in range(100, 127)]}
    capped = await fetch_weibo({"uid": _UID, "max_posts": 20}, store, checkpoint=first.checkpoint)
    assert capped.limited and capped.checkpoint["window_end"] != _NOW.isoformat()
    monkeypatch.setattr(weibo_collect, "_now", lambda: _NOW)
    continued = await fetch_weibo(
        {"uid": _UID, "max_posts": 20}, store, checkpoint=capped.checkpoint
    )
    assert len(continued.rows) == 7 and continued.limited
    old_end = datetime.fromisoformat(capped.checkpoint["window_end"])
    assert continued.checkpoint["window_start"] == (old_end - timedelta(days=7)).isoformat()
    assert datetime.fromisoformat(continued.checkpoint["window_end"]) - old_end <= timedelta(
        days=365
    )


async def test_old_pinned_never_stops_newer_page(fixture, store):
    fixture.pages = {1: [post(90, days=300, isTop=1), post(100)], 2: [post(101)]}
    batch = await fetch_weibo({"uid": _UID}, store)
    assert [row["id"] for row in batch.rows] == ["100", "101"]
    assert batch.complete and batch.pages == 3


async def test_page_of_only_pins_does_not_end_window(fixture, store):
    fixture.pages = {1: [post(90, days=300, isTop=1)], 2: [post(101)]}
    assert len((await fetch_weibo({"uid": _UID}, store)).rows) == 1


async def test_old_ordinary_page_ends_window(fixture, store):
    fixture.pages = {1: [post(90, days=40)]}
    batch = await fetch_weibo({"uid": _UID}, store)
    assert batch.complete and not batch.rows and batch.pages == 1


async def test_cap_continues_same_page_without_dropping_posts(fixture, store):
    fixture.pages = {1: [post(i) for i in range(100, 127)]}
    first = await fetch_weibo({"uid": _UID, "max_posts": 20}, store)
    saved = copy.deepcopy(first.checkpoint)
    assert first.limited and not first.complete and len(first.rows) == 20
    assert saved["next_page"] == 1 and len(saved["page_done"]) == 20
    second = await fetch_weibo({"uid": _UID, "max_posts": 20}, store, checkpoint=saved)
    assert saved == first.checkpoint  # fetch never mutates caller's checkpoint
    assert second.complete and len(second.rows) == 7
    assert len({row["id"] for row in first.rows + second.rows}) == 27
    assert second.checkpoint["window_start"] == first.checkpoint["window_start"]


async def test_large_chinese_batch_stays_importable_and_resumes_current_post(fixture, store):
    fixture.pages = {
        page + 1: [post(100 + page * 2 + offset, text_raw="中" * 100_000) for offset in range(2)]
        for page in range(20)
    }
    first = await fetch_weibo({"uid": _UID}, store)
    assert first.limited and not first.complete and 20 < len(first.rows) < 40
    payload = b"".join(weibo_collect.serialize_weibo_row(row) for row in first.rows)
    assert len(payload) <= weibo_collect._MAX_BATCH_BYTES < MAX_FILE_BYTES
    assert all(len(line) < MAX_LINE_BYTES for line in payload.splitlines())
    assert parse_jsonl(payload) == first.rows
    # The blocked row on a partially processed page must not be checkpointed as done.
    next_id = str(100 + len(first.rows))
    assert next_id not in first.checkpoint["page_done"]
    second = await fetch_weibo({"uid": _UID}, store, checkpoint=first.checkpoint)
    assert second.complete and not second.limited and second.rows[0]["id"] == next_id
    assert len({row["id"] for row in first.rows + second.rows}) == 40
    assert parse_jsonl(b"".join(weibo_collect.serialize_weibo_row(row) for row in second.rows))


async def test_single_large_chinese_repost_fails_without_empty_limited_loop(fixture, store):
    fixture.pages = {
        1: [
            post(
                text_raw="中" * 100_000,
                retweeted_status=post(
                    200,
                    text_raw="文" * 100_000,
                    user={"id": 9999, "screen_name": "原作者"},
                ),
            )
        ]
    }
    with pytest.raises(WeiboCollectError) as error:
        await fetch_weibo({"uid": _UID}, store)
    assert error.value.code == "row_too_large"
    assert error.value.public_message == "微博单条完整资料超过导入大小限制，已保留已有资料"


async def test_first_row_exceeding_batch_budget_is_fixed_error(fixture, store, monkeypatch):
    monkeypatch.setattr(weibo_collect, "_MAX_BATCH_BYTES", 10)
    with pytest.raises(WeiboCollectError) as error:
        await fetch_weibo({"uid": _UID}, store)
    assert error.value.code == "row_too_large"


async def test_shifted_resumed_page_keeps_bounded_valid_checkpoint(fixture, store):
    fixture.pages = {1: [post(i) for i in range(100, 127)]}
    first = await fetch_weibo({"uid": _UID, "max_posts": 20}, store)
    fixture.pages[1] = [post(i, days=-1) for i in range(1000, 1180)] + [
        post(i) for i in range(120, 140)
    ]
    second = await fetch_weibo({"uid": _UID, "max_posts": 20}, store, checkpoint=first.checkpoint)
    assert len(second.checkpoint["page_done"]) <= 200
    assert len(second.rows) == 20
    third = await fetch_weibo({"uid": _UID, "max_posts": 20}, store, checkpoint=second.checkpoint)
    assert third.complete and not third.rows
    assert len({row["id"] for row in first.rows + second.rows}) == 40


async def test_longtext_replaces_truncated_text_and_preserves_links(fixture, store, tmp_path):
    fixture.pages = {1: [post(isLongText=True, text_raw="截断…")]}
    fixture.details["Ab100"] = {
        "longTextContent": '<p>完整<a href="https://weibo.com/public">链接</a><br>尾文</p>',
    }
    batch = await fetch_weibo({"uid": _UID}, store)
    assert "截断" not in batch.rows[0]["text"]
    assert "尾文" in batch.rows[0]["text"] and "https://weibo.com/public" in batch.rows[0]["text"]
    assert normalize(tmp_path, batch.rows)


async def test_repost_only_reference_and_media_links(fixture, store, tmp_path):
    original = post(200, user={"id": 9999, "screen_name": "引用作者"}, isLongText=True)
    fixture.pages = {
        1: [
            post(
                retweeted_status=original,
                pic_infos={
                    "p1": {"original": {"url": "https://wx1.sinaimg.cn/large/fixture.jpg"}},
                },
            )
        ]
    }
    batch = await fetch_weibo({"uid": _UID}, store)
    assert len(batch.rows) == 1 and batch.rows[0]["author_id"] == _UID
    assert batch.rows[0]["repost"]["author_id"] == "9999"
    assert "完整合成正文" in batch.rows[0]["repost"]["text"]
    assert "fixture.jpg" in batch.rows[0]["text"]
    assert len(normalize(tmp_path, batch.rows)) == 1
    assert not (await fetch_weibo({"uid": _UID, "include_reposts": False}, store)).rows


@pytest.mark.parametrize(
    "extra",
    [
        {"visible": {"type": 1}},
        {"visible": {}},
        {"visible": {"type": False}},
        {"is_paid": True},
        {"mblog_vip_type": 1},
        {"ad_info": {"x": 1}},
        {"promotion": {"type": "ad"}},
        {"retweeted_status": {"visible": {"type": 1}}},
    ],
)
async def test_nonpublic_paid_ad_not_collected(fixture, store, extra):
    fixture.pages = {1: [post(**extra)]}
    batch = await fetch_weibo({"uid": _UID}, store)
    assert not batch.rows and batch.skipped == 1


async def test_mixed_author_fails_whole_batch(fixture, store):
    fixture.pages = {1: [post(), post(101, user={"id": 999, "screen_name": "other"})]}
    with pytest.raises(WeiboCollectError, match="其他作者") as error:
        await fetch_weibo({"uid": _UID}, store)
    assert error.value.code == "author_mismatch"


@pytest.mark.parametrize(
    "data",
    [
        [],
        {},
        {"list": {}},
        {"list": [None], "since_id": "1"},
        {"list": [post()], "since_id": "bad\ncursor"},
    ],
)
async def test_unknown_schema_fixed_error(fixture, store, data):
    fixture.timeline_data = data
    with pytest.raises(WeiboCollectError) as error:
        await fetch_weibo({"uid": _UID}, store)
    assert error.value.code == "schema_changed"


async def test_detail_failure_cannot_return_truncated_overwrite(fixture, store):
    fixture.pages = {1: [post(isLongText=True, text_raw="bad truncated")]}
    fixture.details["Ab100"] = {}
    with pytest.raises(WeiboCollectError) as error:
        await fetch_weibo({"uid": _UID}, store)
    assert error.value.code == "detail_unavailable"


@pytest.mark.parametrize(
    "cursor",
    [
        "Ab01234567890123456",
        "a" * 100,
        "opaque+/=:%?&value",
        "opaque with space",
        "0",
        "https://never-fetch.invalid/path?token=synthetic",
    ],
)
async def test_opaque_cursor_roundtrips_through_checkpoint_and_encoded_params(
    fixture, store, monkeypatch, cursor
):
    monkeypatch.setattr(weibo_collect, "_MAX_PAGES", 1)

    def timeline(request):
        if request.url.path == "/":
            return fixture(request)
        fixture.calls.append(request)
        assert request.url.host == "weibo.com"
        assert request.url.path == "/ajax/statuses/mymblog"
        page = int(request.url.params["page"])
        if page == 1:
            data = {"list": [post(100)], "since_id": cursor}
        elif page == 2:
            assert request.url.params["since_id"] == cursor
            data = {"list": [post(101)], "since_id": cursor + "next"}
        else:
            data = {"list": []}  # An exhausted endpoint may omit since_id.
        return httpx.Response(200, json={"ok": 1, "data": data})

    fixture.transport = httpx.MockTransport(timeline)
    first = await fetch_weibo({"uid": _UID}, store)
    assert first.limited and first.checkpoint["cursor"] == cursor
    monkeypatch.setattr(weibo_collect, "_MAX_PAGES", 40)
    second = await fetch_weibo({"uid": _UID}, store, checkpoint=first.checkpoint)
    assert second.complete and [row["id"] for row in second.rows] == ["101"]
    assert all(request.url.host == "weibo.com" for request in fixture.calls)


@pytest.mark.parametrize(
    "data", [{"list": []}, {"list": [], "since_id": None}, {"list": [], "since_id": ""}]
)
async def test_empty_terminal_page_does_not_require_cursor(fixture, store, data):
    fixture.timeline_data = data
    batch = await fetch_weibo({"uid": _UID}, store)
    assert batch.complete and not batch.limited and not batch.rows


@pytest.mark.parametrize(
    "cursor", [None, True, {}, [], "bad\ncursor", "bad\x7fcursor", "中文", "x" * 513]
)
async def test_nonempty_page_cursor_remains_bounded_and_ascii(fixture, store, cursor):
    fixture.timeline_data = {"list": [post()], "since_id": cursor}
    with pytest.raises(WeiboCollectError) as error:
        await fetch_weibo({"uid": _UID}, store)
    assert error.value.code == "schema_changed"


async def test_repeated_page_is_not_success(fixture, store):
    fixture.pages = {1: [post()], 2: [post()]}
    with pytest.raises(WeiboCollectError) as error:
        await fetch_weibo({"uid": _UID}, store)
    assert error.value.code == "pagination_stalled"


async def test_invalid_checkpoint_rejected_before_http(fixture, store):
    batch = await fetch_weibo({"uid": _UID}, store)
    for update in (
        {"uid": "9999"},
        {"next_page": True},
        {"window_start": "bad"},
        {"cursor": "bad\ncursor"},
        {"cursor": "x" * 513},
        {"page_done": ["../secret"]},
    ):
        with pytest.raises(WeiboCollectError) as error:
            await fetch_weibo({"uid": _UID}, store, checkpoint={**batch.checkpoint, **update})
        assert error.value.code == "invalid_checkpoint"


async def test_page_budget_is_resumable(fixture, store, monkeypatch):
    monkeypatch.setattr(weibo_collect, "_MAX_PAGES", 1)
    fixture.pages = {1: [post()], 2: [post(101)]}
    first = await fetch_weibo({"uid": _UID}, store)
    assert first.limited and first.checkpoint["next_page"] == 2
    second = await fetch_weibo({"uid": _UID}, store, checkpoint=first.checkpoint)
    assert second.rows[0]["id"] == "101"


async def test_request_budget_during_detail_keeps_current_post_pending(fixture, store, monkeypatch):
    monkeypatch.setattr(weibo_client, "_MAX_REQUESTS", 2)
    fixture.pages = {1: [post(isLongText=True)]}
    batch = await fetch_weibo({"uid": _UID}, store)
    assert batch.limited and not batch.rows
    assert batch.checkpoint["next_page"] == 1 and not batch.checkpoint["page_done"]


@pytest.mark.parametrize(
    "status,code",
    [
        (302, "authentication_required"),
        (403, "risk_control"),
        (429, "rate_limited"),
        (500, "network"),
    ],
)
async def test_http_failures_are_fixed_and_bounded(fixture, store, status, code, caplog):
    fixture.status = status
    with caplog.at_level(logging.DEBUG), pytest.raises(WeiboCollectError) as error:
        await fetch_weibo({"uid": _UID}, store)
    assert error.value.code == code
    assert "secret" not in str(error.value)
    assert _COOKIE not in caplog.text
    assert "uid=" not in caplog.text
    assert len(fixture.calls) <= 4


@pytest.mark.parametrize(
    "html",
    [
        "<html>login</html>",
        '<script>window.$CONFIG={"uid":"12345"}</script>',
        '<script>window.$CONFIG={"uid":"12345","user":{"id":99999}}</script>',
        '<script>window.$CONFIG={"uid":"0","user":{"id":0}}</script>',
        _VIEWER + _VIEWER,
    ],
)
async def test_cannot_treat_200_or_cookie_as_identity(fixture, store, html):
    fixture.identity = html
    with pytest.raises(WeiboCollectError) as error:
        await fetch_weibo({"uid": _UID}, store)
    assert error.value.code == "authentication_required"
    assert len(fixture.calls) == 1


async def test_default_client_disables_ambient_proxy(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9")
    async with WeiboClient([]) as client:
        assert not client._client._trust_env
        assert not client._client._mounts
        assert not client._client.follow_redirects


async def test_response_limit_applies_streamed_body(monkeypatch):
    monkeypatch.setattr(weibo_client, "_MAX_BODY", 40)
    async with WeiboClient(
        [], transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"x" * 41))
    ) as client:
        with pytest.raises(WeiboCollectError) as error:
            await client.verify_identity()
        assert error.value.code == "response_too_large"


async def test_cancel_inflight_closes_client_and_propagates(fixture, store):
    entered = asyncio.Event()

    async def waiting(_: httpx.Request) -> httpx.Response:
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    fixture.transport = httpx.MockTransport(waiting)
    task = asyncio.create_task(fetch_weibo({"uid": _UID}, store))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert fixture.clients[0]._client.is_closed


async def test_missing_credentials_no_network(fixture, tmp_path):
    with pytest.raises(WeiboCollectError) as error:
        await fetch_weibo({"uid": _UID}, WeiboCredentialStore(tmp_path / "config"))
    assert error.value.code == "authentication_required" and not fixture.calls


def test_credential_read_is_filtered_copy(store):
    values = store.read_for_collection()
    values[0]["value"] = "changed"
    assert store.read_for_collection()[0]["value"] == _COOKIE
    assert _COOKIE not in json.dumps(store.status())


def test_collection_credential_read_rejects_world_readable_file(store):
    store.path.chmod(0o644)
    with pytest.raises(ValueError, match="服务端会话不可用"):
        store.read_for_collection()


def test_collection_credential_read_rejects_symlink(store, tmp_path):
    real = store.path
    link = tmp_path / "credential-link"
    link.symlink_to(real)
    store.path = link
    with pytest.raises(ValueError, match="服务端会话不可用"):
        store.read_for_collection()


@pytest.mark.parametrize(
    "script",
    [
        'window.$CONFIG={"uid":"12345","user":{"id":12345}};window.$CONFIG={"uid":""}',
        'try{window.$CONFIG={"uid":"12345","user":{"id":12345}};'
        'window.$CONFIG={"uid":""}}catch(e){}',
        'try{window.$CONFIG={"uid":"12345","user":{"id":12345}}}catch(e){'
        'window.$CONFIG={"uid":"99999","user":{"id":99999}}}',
        'try{window.$CONFIG={"uid":"12345","user":{"id":12345}};throw Error()}catch(e){'
        'window.$CONFIG={"uid":""}}',
        'try{window.$CONFIG={"uid":"12345","user":{"id":12345}}}finally{window.$CONFIG={"uid":""}}',
        'window.$CONFIG={"uid":"12345","user":{"id":12345}};window.$CONFIG=null',
        'if(false){window.$CONFIG={"uid":"12345","user":{"id":12345}}}',
    ],
)
def test_identity_rejects_independent_overwrites_and_unsafe_control_flow(script):
    assert not viewer_confirmed(f"<script>{script}</script>".encode())


@pytest.mark.parametrize("name", ["e", "error", "_failure", "$error"])
async def test_official_root_try_catch_empty_fallback_with_global_prefix(fixture, store, name):
    fixture.identity = (
        '<script>window.PUBLIC_ENV={"feature":true,"entry":"synthetic"};\n'
        'try { window.$CONFIG = {"uid":"12345","user":{"id":12345},'
        '"name":"JSON text with }; and catch(e){}"}; } '
        f"catch ({name}) {{ window.$CONFIG = {{}}; }} </script>"
    )
    assert viewer_confirmed(fixture.identity.encode())
    batch = await fetch_weibo({"uid": _UID}, store)
    assert batch.complete and len(batch.rows) == 1


@pytest.mark.parametrize(
    "fallback",
    [
        '{"uid":""}',
        '{"user":{}}',
        '{"uid":0,"user":{"id":0}}',
        '{"uid":"12345","user":{"id":12345}}',
        "null",
        "[]",
    ],
)
def test_catch_fallback_must_be_the_observed_empty_object(fallback):
    script = (
        'window.PUBLIC_ENV={};try{window.$CONFIG={"uid":"12345","user":{"id":12345}};}'
        f"catch(e){{window.$CONFIG={fallback};}}"
    )
    assert not viewer_confirmed(f"<script>{script}</script>".encode())


@pytest.mark.parametrize(
    "suffix",
    [
        ";window.$CONFIG={}",
        ';window["$CONFIG"]={}',
        ";window.$CONFIG=null",
        "finally{window.$CONFIG={}}",
    ],
)
def test_fallback_wrapper_cannot_hide_later_overwrite(suffix):
    script = (
        'window.PUBLIC_ENV={};try{window.$CONFIG={"uid":"12345","user":{"id":12345}};}'
        "catch(e){window.$CONFIG={};}" + suffix
    )
    assert not viewer_confirmed(f"<script>{script}</script>".encode())


async def test_root_static_bootstrap_suffix_is_not_executed(fixture, store):
    fixture.identity = (
        '<script>window.PUBLIC_ENV={"feature":true};'
        'try{window.$CONFIG={"uid":"12345","user":{"id":12345}};}'
        "catch(e){window.$CONFIG={};}"
        'const s=document.createElement("script");'
        's.src="https://h5.sinaimg.cn/public/synthetic.js";'
        "document.body.appendChild(s);</script>"
    )
    assert viewer_confirmed(fixture.identity.encode())
    result = await fetch_weibo({"uid": _UID}, store)
    assert result.complete and len(result.rows) == 1
    assert all(request.url.host == "weibo.com" for request in fixture.calls)


@pytest.mark.parametrize(
    "suffix",
    [
        'eval("arbitrary")',
        'const s=document.createElement("script");s.src="javascript:bad";'
        "document.body.appendChild(s);",
        'const s=document.createElement("script");s.src="https://static.example/test.js";'
        "document.body.appendChild(other);",
        'const s=document.createElement("script");s.src=window.$CONFIG;'
        "document.body.appendChild(s);",
    ],
)
def test_root_bootstrap_must_be_literal_and_must_not_reference_config(suffix):
    script = (
        'try{window.$CONFIG={"uid":"12345","user":{"id":12345}};}'
        "catch(e){window.$CONFIG={};}" + suffix
    )
    assert not viewer_confirmed(f"<script>{script}</script>".encode())


@pytest.mark.parametrize(
    "mutation",
    [
        'window.$CONFIG.uid="99999";',
        'window.$CONFIG.user.id="99999";',
        'window["$CONFIG"].uid="99999";',
        'window.$CONFIG["user"]={"id":"99999"};',
    ],
)
def test_other_script_config_member_override_is_rejected(mutation):
    assert not viewer_confirmed((_VIEWER + f"<script>{mutation}</script>").encode())


@pytest.mark.parametrize(
    "url",
    [
        "http://static.example.invalid/asset.js",
        "https://user:password@static.example.invalid/asset.js",
        "https://static.example.invalid:8443/asset.js",
    ],
)
def test_bootstrap_url_requires_fixed_safe_https_literal(url):
    script = (
        'try{window.$CONFIG={"uid":"12345","user":{"id":12345}};}'
        'catch(e){window.$CONFIG={};} const x=document.createElement("script");'
        f'x.src="{url}";document.body.appendChild(x);'
    )
    assert not viewer_confirmed(f"<script>{script}</script>".encode())


def test_duplicate_viewer_json_keys_rejected():
    assert not viewer_confirmed(
        b'<script>window.$CONFIG={"uid":"12345","uid":"12345","user":{"id":12345}}</script>'
    )


def test_viewer_valid_fixture_only():
    assert viewer_confirmed(_VIEWER.encode())
    assert not viewer_confirmed(
        b'<script>try{window.$CONFIG={"uid":"12345","user":{"id":12345}};'
        b'window.$CONFIG={"uid":"99999","user":{"id":99999}}}catch(e){}</script>'
    )


class ChunkStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b"x" * 20
        yield b"y" * 21


async def test_unknown_length_stream_response_bound(monkeypatch):
    monkeypatch.setattr(weibo_client, "_MAX_BODY", 40)
    monkeypatch.setattr(weibo_client, "_MIN_INTERVAL", 0)
    async with WeiboClient(
        [], transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=ChunkStream()))
    ) as client:
        with pytest.raises(WeiboCollectError) as error:
            await client.verify_identity()
        assert error.value.code == "response_too_large"


async def test_deep_json_is_fixed_error(monkeypatch):
    monkeypatch.setattr(weibo_client, "_MIN_INTERVAL", 0)
    async with WeiboClient(
        [],
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, content=b"[" * 2000 + b"]" * 2000)
        ),
    ) as client:
        with pytest.raises(WeiboCollectError) as error:
            await client.json("/ajax/statuses/mymblog", {"uid": _UID})
        assert error.value.code == "schema_changed"


async def test_transport_error_retry_has_no_sensitive_exception(monkeypatch, caplog):
    monkeypatch.setattr(weibo_client, "_MIN_INTERVAL", 0)
    count = 0

    def failing(request):
        nonlocal count
        count += 1
        raise httpx.ReadError(_COOKIE, request=request)

    async with WeiboClient([], transport=httpx.MockTransport(failing)) as client:
        with caplog.at_level(logging.DEBUG), pytest.raises(WeiboCollectError) as error:
            await client.verify_identity()
        assert error.value.code == "network" and count == 3
        assert _COOKIE not in str(error.value) and _COOKIE not in caplog.text


async def test_total_runtime_budget_keeps_checkpoint_and_closes(fixture, store, monkeypatch):
    monkeypatch.setattr(weibo_collect, "_MAX_SECONDS", 0.02)

    async def hanging(request):
        if request.url.path == "/":
            return fixture(request)
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    fixture.transport = httpx.MockTransport(hanging)
    batch = await fetch_weibo({"uid": _UID}, store)
    assert batch.limited and not batch.complete and not batch.rows
    assert batch.checkpoint["next_page"] == 1
    assert fixture.clients[0]._client.is_closed


async def test_created_at_timezone_and_not_date_in_text(fixture, store, tmp_path):
    fixture.pages = {
        1: [
            post(
                text_raw="活动时间2027-12-12不是发表日期",
                created_at="Thu Nov 26 23:30:00 +0000 2026",
            )
        ]
    }
    row = (await fetch_weibo({"uid": _UID}, store)).rows[0]
    assert row["published_at"] == "2026-11-27T07:30:00+08:00"
    assert normalize(tmp_path, [row])[0].date == "2026-11-27"


async def test_longtext_markdown_not_parsed_as_html(fixture, store):
    fixture.pages = {1: [post(isLongText=True)]}
    fixture.details["Ab100"] = {"longTextContent": "# 标题\n\n<angle>完整正文", "isMarkdown": True}
    batch = await fetch_weibo({"uid": _UID}, store)
    assert batch.rows[0]["text"] == "# 标题\n\n<angle>完整正文"


@pytest.mark.parametrize("cursor", ["1000", "opaque+/=AbC", "0"])
async def test_cursor_repetition_is_fixed_stop(fixture, store, cursor):
    calls = 0

    def repeated_cursor(request):
        nonlocal calls
        if request.url.path == "/":
            return fixture(request)
        calls += 1
        return httpx.Response(
            200,
            json={
                "ok": 1,
                "data": {"list": [post(100 + calls)], "since_id": cursor},
            },
        )

    fixture.transport = httpx.MockTransport(repeated_cursor)
    with pytest.raises(WeiboCollectError) as error:
        await fetch_weibo({"uid": _UID}, store)
    assert error.value.code == "pagination_stalled" and calls == 2


async def test_sensitive_http_scope_does_not_redact_unrelated_logs(fixture, store, caplog):
    caplog.set_level(logging.INFO, logger="httpx")
    with caplog.at_level(logging.INFO):
        await fetch_weibo({"uid": _UID}, store)
        logging.getLogger("httpx").info("unrelated HTTP request")
    assert "unrelated HTTP request" in caplog.text
    assert "uid=" not in caplog.text


@pytest.mark.parametrize(
    "values",
    [
        {"uid": _UID, "proxy": "https://evil"},
        {"uid": _UID, "max_posts": True},
        {"uid": "https://evil.com/u/12345"},
    ],
)
async def test_unsafe_settings_fixed_failure_before_network(fixture, store, values):
    with pytest.raises(WeiboCollectError) as error:
        await fetch_weibo(values, store)
    assert error.value.code == "invalid_settings" and not fixture.calls
