"""Native boolean advertising slots are not foreign account posts or date evidence."""

import copy

import pytest

from jianshanagent.weibo_client import WeiboCollectError
from jianshanagent.weibo_collect import _native_ad, _public, fetch_weibo
from jianshanagent.weibo_history import fetch_history_page
from jianshanagent.weibo_history_queue import HistoryQueue
from test_weibo_collect import fixture as fixture
from test_weibo_collect import post as recent_post
from test_weibo_collect import store as store
from test_weibo_history import SETTINGS, STAMP, UID, Credentials, FakeClient, post, queue


def advertisement():
    # Public synthetic native shape. Its author/date/alias must never be normalized.
    return {
        "id": 999,
        "idstr": "999",
        "isAd": True,
        "user": {"id": "12345", "screen_name": "广告槽"},
        "created_at": "not-a-post-date",
        "mblogid": "?",
        "text_raw": "不应持久化",
    }


async def test_recent_native_twenty_owned_plus_foreign_ad_continues(fixture, store):
    fixture.pages = {
        1: [advertisement(), *[recent_post(i, isAd=False) for i in range(100, 120)]],
        2: [recent_post(120, isAd=False)],
    }
    batch = await fetch_weibo({"uid": UID}, store)
    assert batch.complete and batch.skipped == 1
    assert {row["id"] for row in batch.rows} == {str(i) for i in range(100, 121)}
    assert {
        request.url.params["page"]
        for request in fixture.calls
        if request.url.path.endswith("mymblog")
    } == {"1", "2", "3"}


async def test_recent_all_ads_pages_do_not_terminate_or_stall_on_repeated_ad(fixture, store):
    fixture.pages = {1: [advertisement()], 2: [advertisement()], 3: [recent_post()]}
    result = await fetch_weibo({"uid": UID}, store)
    assert result.complete and [row["id"] for row in result.rows] == ["100"]
    assert result.skipped == 2


@pytest.mark.parametrize(
    "flags", [{}, {"isAd": False}, {"isAd": "1"}, {"isAd": "true"}, {"isAd": 1}, {"is_ad": True}]
)
async def test_recent_other_author_is_not_exempt_without_exact_native_boolean(
    fixture, store, flags
):
    foreign = recent_post(user={"id": "12345", "screen_name": "其它作者"}, **flags)
    fixture.pages = {1: [foreign]}
    with pytest.raises(WeiboCollectError) as caught:
        await fetch_weibo({"uid": UID}, store)
    assert caught.value.code == "author_mismatch"


async def test_history_native_mixed_page_replay_keeps_counts_and_post_cursor(tmp_path):
    q = queue(tmp_path)
    source = {
        "list": [advertisement(), *[post(str(i)) for i in range(100, 120)]],
        "since_id": "one",
    }
    client = FakeClient(pages=[source])
    proposed = await fetch_history_page(q, SETTINGS, Credentials(), client_factory=lambda _: client)
    assert proposed["skipped"] == 1 and len(proposed["post_rows"]) == 20
    assert q.status()["skipped"] == 0 and q.job()["cursor"] == ""
    q.close()
    q = queue(tmp_path)
    assert await fetch_history_page(q, SETTINGS, None) == proposed
    q.acknowledge()
    assert q.status()["skipped"] == 1 and q.status()["posts_total"] == 20
    assert q.job()["cursor"] == "one" and q.job()["page"] == 2
    next_client = FakeClient(pages=[{"list": [post("120")], "since_id": "two"}])
    await fetch_history_page(q, SETTINGS, Credentials(), client_factory=lambda _: next_client)
    q.acknowledge()
    assert q.status()["posts_total"] == 21 and q.status()["skipped"] == 1
    assert not q.db.execute("SELECT 1 FROM posts WHERE id='999'").fetchone()
    q.close()


async def test_history_partial_page_deduplicates_native_ad_skip_count(tmp_path):
    q = queue(tmp_path)
    source = {"list": [advertisement(), post("100"), post("101")], "since_id": "next"}
    for expected in ("100", "101"):
        client = FakeClient(pages=[source])
        proposed = await fetch_history_page(
            q,
            {**SETTINGS, "max_posts": 1},
            Credentials(),
            client_factory=lambda _, chosen=client: chosen,
        )
        assert [row["id"] for row in proposed["post_rows"]] == [expected]
        q.acknowledge()
    assert q.status()["posts_total"] == 2 and q.status()["skipped"] == 1
    assert q.job()["cursor"] == "next" and q.job()["page"] == 2
    q.close()


async def test_history_ads_only_pages_never_supply_date_or_terminal_evidence(tmp_path):
    q = queue(tmp_path)
    q.stage(q.job(), terminal=True)
    q.acknowledge()
    assert q.verify()
    with q.transaction():
        q.set("incremental_since", "2009-01-01T00:00:00+00:00")
        q.set("catchup_until", "2030-01-01T00:00:00+00:00")
    for cursor in ("one", "two"):
        client = FakeClient(pages=[{"list": [advertisement()], "since_id": cursor}])
        proposed = await fetch_history_page(
            q, SETTINGS, Credentials(), client_factory=lambda _, chosen=client: chosen
        )
        assert not proposed["terminal"] and proposed["post_rows"] == []
        q.acknowledge()
    assert q.status()["skipped"] == 2 and q.status()["posts_total"] == 0
    # Ad-only pages supply no watermark evidence: catchup bounds stay frozen.
    assert q.get("incremental_since") == "2009-01-01T00:00:00+00:00"
    assert q.get("catchup_until") == "2030-01-01T00:00:00+00:00"
    q.close()


async def test_history_target_self_mixed_ad_page_keeps_discussion_lane_clean(tmp_path):
    q = HistoryQueue(
        tmp_path / "history.sqlite3", UID, "signature", now=STAMP, discussion_scope="target_self"
    )
    source = {
        "list": [advertisement(), *[post(str(i)) for i in range(100, 120)]],
        "since_id": "one",
    }
    settings = {**SETTINGS, "discussion_scope": "target_self"}
    client = FakeClient(pages=[source])
    proposed = await fetch_history_page(
        q, settings, Credentials(), client_factory=lambda _: client
    )
    assert proposed["skipped"] == 1 and len(proposed["post_rows"]) == 20
    q.acknowledge()
    assert q.status()["skipped"] == 1 and q.status()["posts_total"] == 20
    assert not q.db.execute("SELECT 1 FROM posts WHERE id='999'").fetchone()
    comment_keys = {row[0] for row in q.db.execute("SELECT key FROM jobs WHERE kind='comments'")}
    assert comment_keys == {str(i) for i in range(100, 120)}
    q.close()


@pytest.mark.parametrize(
    "flags", [{}, {"isAd": False}, {"isAd": "1"}, {"isAd": "true"}, {"isAd": 1}, {"is_ad": True}]
)
async def test_history_other_author_without_native_boolean_still_blocks(tmp_path, flags):
    q = queue(tmp_path)
    foreign = post()
    foreign["user"]["id"] = "12345"
    foreign.update(flags)
    client = FakeClient(pages=[{"list": [foreign], "since_id": "next"}])
    with pytest.raises(WeiboCollectError) as caught:
        await fetch_history_page(q, SETTINGS, Credentials(), client_factory=lambda _: client)
    assert caught.value.code == "author_mismatch"
    assert q.pending() is None and q.job()["cursor"] == ""
    q.close()


def test_only_native_bool_is_ad_and_public_filter_covers_reposts():
    original = recent_post()
    assert _public(original)
    for marker in (False, None, 1, "1", "true"):
        candidate = copy.deepcopy(original)
        candidate["isAd"] = marker
        assert not _native_ad(candidate)
    original["isAd"] = True
    assert _native_ad(original) and not _public(original)
