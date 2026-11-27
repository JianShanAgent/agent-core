"""Synthetic offline history/queue tests; no real author data or credentials."""

import asyncio
import copy
import json
from types import SimpleNamespace

import httpx
import pytest

from jianshanagent.core.config import AppConfig
from jianshanagent.plugins.knowledge import source_collect as control
from jianshanagent.plugins.knowledge.source_collect_runner import SourceCollectorRunner
from jianshanagent.weibo_client import WeiboClient, WeiboCollectError
from jianshanagent.weibo_history import (
    _page_comments,
    discussion_node,
    fetch_history_page,
    measure_resources,
)
from jianshanagent.weibo_history_queue import HistoryQueue
from test_source_import import conf

UID = "5896401674"
STAMP = 1_800_000_000.0
SETTINGS = {
    "uid": UID,
    "mode": "full_then_incremental",
    "include_comments": True,
    "include_replies": True,
    "discussion_source_id": "social_comments",
    "max_posts": 200,
}
DATE = "Fri Jan 01 00:00:00 +0800 2010"


def post(native="100"):
    return {
        "id": native,
        "mblogid": f"Abc{native}",
        "user": {"id": UID, "screen_name": "测试目标"},
        "created_at": DATE,
        "text_raw": "完整历史测试正文",
        "visible": {"type": 0},
    }


def row(native="100"):
    return {
        "version": 1,
        "id": native,
        "author_id": UID,
        "author_name": "测试目标",
        "text": "完整历史测试正文",
        "url": f"https://weibo.com/{UID}/Abc{native}",
        "published_at": "2010-01-01T00:00:00+08:00",
    }


def comment(native="200", *, root=None, total=0, text="完整合成评论"):
    value = {
        "id": native,
        "user": {"id": "12345", "screen_name": "合成测试用户"},
        "created_at": DATE,
        "text_raw": text,
        "total_number": total,
        "comments": [],
    }
    if root:
        value["rootid"] = root
    return value


def queue(tmp_path):
    return HistoryQueue(tmp_path / "history.sqlite3", UID, "signature", now=STAMP)


def discussion_rows(q):
    return [row for lane, rows in q.publications() if lane == "discussions" for row in rows]


def seed_posts(q, values=None):
    q.stage(q.job(), post_rows=[row()] if values is None else values, terminal=True)
    assert q.status()["posts_total"] == 0
    q.acknowledge(now=STAMP)
    assert q.get("history_phase") == "comments"


class Credentials:
    def read_for_collection(self):
        return []


class FakeClient:
    def __init__(self, *, pages=None, envelopes=None):
        self.pages = pages or []
        self.envelopes = envelopes or []
        self.params = []
        self.calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def verify_identity(self):
        pass

    async def json(self, path, params):
        self.params.append(params)
        return self.pages.pop(0)

    async def comments(self, *args, **kwargs):
        self.calls += 1
        self.params.append(kwargs)
        return self.envelopes.pop(0)


async def test_full_history_has_no_365_day_floor_and_more_than_7000_posts(tmp_path):
    q = queue(tmp_path)
    values = [post(str(10000 + i)) for i in range(7001)]
    pages = [{"list": values[i : i + 200], "since_id": str(i + 1)} for i in range(0, 7001, 200)]
    pages.append({"list": []})
    accepted = 0
    while q.get("history_phase") == "posts":
        # A resumed partial page is requested again until its page_done commits.
        current = pages[0]
        client = FakeClient(pages=[current])
        pending = await fetch_history_page(
            q, SETTINGS, Credentials(), client_factory=lambda _, chosen=client: chosen
        )
        accepted += len(pending["post_rows"])
        assert len(pending["post_rows"]) <= 200
        advance = pending["advance_page"]
        q.acknowledge(now=STAMP)
        if advance:
            pages.pop(0)
        with q.transaction():
            q.set("measurement", {"evaluated": True})
    assert accepted == q.status()["posts_total"] == 7001
    assert q.status()["pending_comments"] == 7001
    assert q.status()["oldest_date"].startswith("2010-")
    assert not q.status()["history_complete"]
    assert len(json.dumps(q.status())) < 2000
    q.close()


async def test_pending_replays_after_restart_and_cursor_only_advances_after_ack(tmp_path):
    q = queue(tmp_path)
    client = FakeClient(pages=[{"list": [post()], "since_id": "next"}])
    candidate = await fetch_history_page(
        q, SETTINGS, Credentials(), client_factory=lambda _, chosen=client: chosen
    )
    assert q.job()["cursor"] == ""
    q.close()
    q = queue(tmp_path)
    replay = await fetch_history_page(q, SETTINGS, None)
    assert replay == candidate
    q.acknowledge(now=STAMP)
    assert q.job()["cursor"] == "next"
    assert q.status()["posts_total"] == 1
    q.close()


async def test_partial_page_max_posts_resume_does_not_skip_long_page(tmp_path):
    q = queue(tmp_path)
    settings = {**SETTINGS, "max_posts": 1}
    for expected in ("100", "101", "102"):
        client = FakeClient(
            pages=[{"list": [post("100"), post("101"), post("102")], "since_id": "next"}]
        )
        candidate = await fetch_history_page(
            q, settings, Credentials(), client_factory=lambda _, chosen=client: chosen
        )
        assert [r["id"] for r in candidate["post_rows"]] == [expected]
        q.acknowledge(now=STAMP)
    assert q.job()["page"] == 2
    assert q.status()["posts_total"] == 3
    q.close()


def test_stable_packets_merge_inline_pagination_edits_and_restart(tmp_path):
    q = queue(tmp_path)
    seed_posts(q)
    first = comment("200", total=2)
    first["comments"] = [comment("201", root="200")]
    _page_comments(
        q, q.job(), {"data": [first, comment("300")], "max_id": "50", "total_number": 4}, SETTINGS
    )
    assert len(discussion_rows(q)) == 1  # Several short roots share one vector packet.
    identity = discussion_rows(q)[0]["id"]
    assert q.status()["comments_total"] == q.status()["replies_total"] == 0
    q.close()
    q = queue(tmp_path)
    assert discussion_rows(q)[0]["id"] == identity
    q.acknowledge(now=STAMP)
    job = q.job()
    assert job["kind"] == "replies"  # New reply queue is fair, not starved behind older pages.
    _page_comments(
        q,
        job,
        {
            "data": [comment("201", root="200", text="修订完整文本"), comment("202", root="200")],
            "max_id": "0",
            "total_number": 2,
        },
        SETTINGS,
    )
    assert discussion_rows(q)[0]["id"] == identity
    assert len({n["id"] for n in discussion_rows(q)[0]["nodes"]}) == 4
    q.acknowledge(now=STAMP)
    assert q.status()["comments_total"] == 2
    assert q.status()["replies_total"] == 2
    q.close()


@pytest.mark.parametrize("root_proven", [False, True])
def test_seven_of_eight_needs_explicit_root_evidence(tmp_path, root_proven):
    q = queue(tmp_path)
    seed_posts(q)
    root = comment(total=8)
    _page_comments(q, q.job(), {"data": [root], "max_id": 0, "total_number": 8}, SETTINGS)
    q.acknowledge(now=STAMP)
    envelope = {
        "data": [comment(str(201 + i), root="200") for i in range(7)],
        "max_id": 0,
        "total_number": 8,
    }
    if root_proven:
        envelope["rootComment"] = root
    _page_comments(q, q.job(), envelope, SETTINGS)
    q.acknowledge(now=STAMP)
    assert not q.verify()  # rootComment context never justifies adding one to the count.
    assert not q.status()["history_complete"]
    assert q.status()["count_mismatches"] == 1
    q.close()


def test_189_reported_with_only_twenty_roots_does_not_complete(tmp_path):
    q = queue(tmp_path)
    seed_posts(q)
    _page_comments(
        q,
        q.job(),
        {"data": [comment(str(200 + i)) for i in range(20)], "max_id": 0, "total_number": 189},
        SETTINGS,
    )
    q.acknowledge(now=STAMP)
    assert not q.verify()
    assert q.status()["count_mismatches"] == 1
    q.pause(True, reason="count_mismatch")
    q.pause(False)
    assert q.job()["kind"] == "comments"
    assert q.job()["cursor"] == ""
    q.close()


def test_cross_post_and_cross_root_rejected_and_pending_not_overwritten(tmp_path):
    q = queue(tmp_path)
    seed_posts(q)
    value = comment()
    value["status"] = {"id": "999"}
    with pytest.raises(WeiboCollectError, match="其他作者"):
        discussion_node(value, "100")
    value = comment("201", root="999")
    with pytest.raises(WeiboCollectError):
        discussion_node(value, "100", root_id="200")
    _page_comments(q, q.job(), {"data": [comment()], "max_id": 0, "total_number": 1}, SETTINGS)
    saved = q.pending()
    with pytest.raises(WeiboCollectError):
        _page_comments(q, q.job(), {"data": [], "max_id": 0, "total_number": 0}, SETTINGS)
    assert q.pending() == saved
    q.close()


def test_64_node_and_text_bounds_and_no_full_tree_status(tmp_path):
    q = queue(tmp_path)
    seed_posts(q)
    values = [comment(str(200 + i), text="合成" * 3000) for i in range(70)]
    _page_comments(q, q.job(), {"data": values, "max_id": 0, "total_number": 70}, SETTINGS)
    assert len(discussion_rows(q)) > 1
    for packet in discussion_rows(q):
        assert len(packet["nodes"]) <= 64
        assert sum(len(n["text"]) for n in packet["nodes"]) <= 100_000
    assert "nodes" not in q.status()
    q.close()


def test_resource_budget_accounts_pg_growth_and_durable_block(tmp_path, monkeypatch):
    import jianshanagent.weibo_history as history

    q = queue(tmp_path)
    monkeypatch.setattr(history, "tree_usage", lambda _: (1024, 0))
    monkeypatch.setattr(history.shutil, "disk_usage", lambda _: SimpleNamespace(free=10 * 1024**3))
    initial = measure_resources(q, [tmp_path], pg_bytes=1024)
    assert (
        initial["resource_bytes"] == 1024
    )  # Existing PG growth is not hidden in a new local baseline.
    blocked = measure_resources(q, [tmp_path], pg_bytes=4 * 1024**3)
    assert blocked["blocked"]
    assert q.status()["paused"] and q.status()["history_phase"] == "blocked"
    q.close()
    q = queue(tmp_path)
    assert q.get("block_reason") == "resource_limit"
    q.close()


def full_config(tmp_path, enabled=True):
    old = conf(tmp_path)
    files = copy.deepcopy(old.files)
    source = files["core.sources"]["sources"][0]
    source.update(storage="stream_v2", collector={**SETTINGS, "enabled": enabled})
    files["core.sources"]["sources"].append(
        {
            "id": "social_comments",
            "label": "合成讨论",
            "platform": "weibo",
            "role": "community",
            "discussion_of": "social",
            "storage": "stream_v2",
        }
    )
    return AppConfig(old.root, files, old.secrets)


def test_linked_group_lease_child_controls_and_safe_status(tmp_path):
    config = full_config(tmp_path)
    assert control.source_operation_group(config, "social_comments") == "social"
    with control.acquire_source_operation(config, "social"):
        assert control.source_operation_active(config, "social_comments")
        assert not control.collector_active(
            config, "social"
        )  # Admin's own operation lock is not a collector.
        with pytest.raises(control.SourceCollectError):
            control.acquire_source_operation(config, "social_comments")
    with pytest.raises(control.SourceCollectError) as caught:
        control.request_collect(config, "social_comments")
    assert caught.value.code == "linked_source"
    child = control.collector_overview(config, "social_comments")
    assert child["parent_source_id"] == "social"
    status = control._public(
        {
            "state": "blocked",
            "history_phase": "blocked",
            "history_complete": False,
            "posts_total": -1,
            "measurement": {"disk_bytes": 12, "cookie": "SECRET", "pg_bytes": float("nan")},
            "coverage_message": "SECRET",
        }
    )
    assert "SECRET" not in json.dumps(status)
    assert status["posts_total"] == 0
    assert status["measurement"]["disk_bytes"] == 12


class FakeService:
    def __init__(self, config):
        self.config = config
        self.store = self
        self.resource_probe = self.probe
        self.imported = []
        self.fail = False
        self.hold = None
        self.started = asyncio.Event()

    async def info(self):
        return {}

    async def probe(self, ids):
        return {
            "database_bytes": 1_000_000,
            "wal_position_bytes": 1_000_000,
            "source_rows_bytes": 0,
            "pg_started_at": 12345,
            "pg_disk_free_bytes": 10 * 1024**3,
        }

    async def import_data(self, target, content):
        self.started.set()
        if self.hold:
            await self.hold.wait()
        if self.fail:
            return {"state": "failed"}
        assert control.source_operation_active(self.config, target)
        self.imported.append((target, content))
        return {"state": "completed", "new": len(content.splitlines())}

    def status(self, target):
        return {"state": "importing"}

    def cancel(self, target):
        if self.hold:
            self.hold.set()

    async def aclose(self):
        pass


async def runner_harness(tmp_path, monkeypatch, *, page=None):
    import jianshanagent.weibo_history as history

    config = full_config(tmp_path)
    clock = SimpleNamespace(now=STAMP)
    service = FakeService(config)
    calls = []

    async def factory(config):
        async def close():
            pass

        return service, close

    async def fetch(q, settings, credentials):
        calls.append(q.job())
        if q.pending():
            return q.pending()
        job = q.job()
        if job is None:
            return None
        return q.stage(job, post_rows=[row()] if page is None else page, terminal=True)

    monkeypatch.setattr(history, "fetch_history_page", fetch)
    monkeypatch.setattr(history.shutil, "disk_usage", lambda _: SimpleNamespace(free=10 * 1024**3))
    runner = SourceCollectorRunner(lambda: config, clock=lambda: clock.now, service_factory=factory)
    return config, clock, service, calls, runner


async def finish(runner):
    if runner.tasks:
        await asyncio.gather(*tuple(runner.tasks.values()))
    await asyncio.sleep(0)


async def test_runner_short_batches_no_legacy_complete_migration_and_persistent_pause(
    tmp_path, monkeypatch
):
    config, clock, service, calls, runner = await runner_harness(tmp_path, monkeypatch)
    directory = control._directory(config, "social")
    directory.mkdir(parents=True)
    control._atomic_json(
        directory / "checkpoint.json", {"checkpoint": {"complete": True, "initial_complete": True}}
    )
    await runner.tick()
    await finish(runner)
    status = control.collector_overview(config, "social")
    assert status["state"] == "completed" and not status["history_complete"]
    assert status["history_phase"] == "comments"
    assert status["next_batch_at"] == clock.now + 2
    control.cancel_collect(config, "social", status["job_id"])
    for _ in range(3):
        clock.now += 1800
        await runner.tick()
    assert len(calls) == 1
    queued = control.request_collect(config, "social")
    assert queued["state"] == "queued" and not queued["paused"]
    await runner.tick()
    await finish(runner)
    assert len(calls) == 2
    await runner.aclose()


async def test_runner_failed_import_retains_candidate_and_manual_resume_replays(
    tmp_path, monkeypatch
):
    config, clock, service, calls, runner = await runner_harness(tmp_path, monkeypatch)
    service.fail = True
    await runner.tick()
    await finish(runner)
    status = control.collector_overview(config, "social")
    assert status["state"] == status["history_phase"] == "blocked"
    assert status["posts_total"] == 0 and status["index_pending"] == 1
    clock.now += 9999
    await runner.tick()
    assert len(calls) == 1
    control.request_collect(config, "social")
    service.fail = False
    await runner.tick()
    await finish(runner)
    status = control.collector_overview(config, "social")
    assert status["posts_total"] == 1 and status["index_pending"] == 0
    assert len(service.imported) == 1
    await runner.aclose()


async def test_comments_transport_real_array_contract_and_fixed_query(monkeypatch):
    import jianshanagent.weibo_client as transport

    monkeypatch.setattr(transport, "_MIN_INTERVAL", 0)
    requests = []

    async def handle(request):
        requests.append(request)
        return httpx.Response(
            200, json={"ok": 1, "data": [], "max_id": 0, "total_number": 0, "tip_msg": "SECRET"}
        )

    async with WeiboClient([], transport=httpx.MockTransport(handle)) as client:
        result = await client.comments("100", UID)
        assert result["data"] == [] and "tip_msg" not in result
        await client.comments("100", UID, root_id="200", root_author_id="12345", max_id="999")
        with pytest.raises(WeiboCollectError):
            await client.comments("https://bad.invalid", UID)
    assert requests[0].url.path == "/ajax/statuses/buildComments"
    assert dict(requests[0].url.params) == {
        "id": "100",
        "uid": UID,
        "is_reload": "1",
        "is_show_bulletin": "2",
        "is_mix": "0",
        "count": "20",
        "fetch_level": "0",
        "locale": "zh-CN",
    }
    assert requests[1].url.params["id"] == "200"
    assert requests[1].url.params["uid"] == "12345"
    assert requests[1].url.params["fetch_level"] == requests[1].url.params["is_mix"] == "1"
    assert requests[1].url.params["max_id"] == "999"


@pytest.mark.parametrize(
    "payload",
    [
        {"ok": True, "data": [], "max_id": 0, "total_number": 0},
        {"ok": 1, "data": {}, "max_id": 0, "total_number": 0},
        {"ok": 1, "data": [], "max_id": "http://bad", "total_number": 0},
    ],
)
async def test_comments_transport_rejects_unknown_schema(payload, monkeypatch):
    import jianshanagent.weibo_client as transport

    monkeypatch.setattr(transport, "_MIN_INTERVAL", 0)
    async with WeiboClient(
        [], transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    ) as client:
        with pytest.raises(WeiboCollectError):
            await client.comments("100", UID)


@pytest.mark.parametrize("root_context", [None, {}, [], False, 0])
async def test_empty_native_root_context_normalizes_to_none(root_context, monkeypatch):
    import jianshanagent.weibo_client as transport

    monkeypatch.setattr(transport, "_MIN_INTERVAL", 0)
    payload = {"ok": 1, "data": [], "max_id": 0, "total_number": 0, "rootComment": root_context}
    async with WeiboClient(
        [], transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    ) as client:
        assert (await client.comments("100", UID))["rootComment"] is None


@pytest.mark.parametrize("inline_count,expected", [(1, 1), (1, 2)])
def test_inaccessible_page_cannot_be_cleared_or_replaced_by_counts(
    tmp_path, inline_count, expected
):
    q = queue(tmp_path)
    seed_posts(q)
    root = comment("200", total=expected)
    root["comments"] = [comment(str(201 + i), root="200") for i in range(inline_count)]
    _page_comments(
        q, q.job(), {"data": [root], "max_id": 0, "total_number": 1 + inline_count}, SETTINGS
    )
    q.acknowledge(now=STAMP)
    job = q.job()
    q.inaccessible(job)
    assert not q.verify()
    assert q.status()["inaccessible"] == 1
    q.pause(True, reason="count_mismatch")
    q.pause(False)
    assert q.job()["kind"] == "replies"
    _page_comments(
        q,
        q.job(),
        {
            "data": [comment(str(201 + i), root="200") for i in range(expected)],
            "max_id": 0,
            "total_number": expected,
            "rootComment": [],
        },
        SETTINGS,
    )
    q.acknowledge(now=STAMP + 1)
    assert q.status()["inaccessible"] == 0  # A real successful terminal page clears it.
    q.close()


def test_edit_growth_fragments_keep_logical_identity_and_restart_no_duplicates(tmp_path):
    q = queue(tmp_path)
    seed_posts(q)
    originals = [comment(str(200 + i), text="合成" * 1000) for i in range(16)]
    _page_comments(q, q.job(), {"data": originals, "max_id": 55, "total_number": 16}, SETTINGS)
    first = discussion_rows(q)
    assert len(first) == 1 and first[0]["packet_id"] == "100:200"
    q.acknowledge(now=STAMP)
    mapping = list(map(tuple, q.db.execute("SELECT id,packet FROM nodes ORDER BY id")))
    edited = comment("200", text="长" * 80000)
    _page_comments(q, q.job(), {"data": [edited], "max_id": 0, "total_number": 16}, SETTINGS)
    fragments = discussion_rows(q)
    assert len(fragments) == 2
    assert {f["packet_id"] for f in fragments} == {"100:200"}
    nodes = [node for fragment in fragments for node in fragment["nodes"]]
    assert len(nodes) == len({node["id"] for node in nodes}) == 16
    assert nodes[0]["text"] == "长" * 80000
    assert all(
        sum(len(node["text"]) for node in fragment["nodes"]) <= 100000 for fragment in fragments
    )
    assert mapping == list(map(tuple, q.db.execute("SELECT id,packet FROM nodes ORDER BY id")))
    q.close()
    q = queue(tmp_path)
    assert discussion_rows(q) == fragments
    q.publication_succeeded()  # Atomic whole-logical-group importer succeeded.
    assert list(q.publications()) == []
    q.acknowledge(now=STAMP + 1)
    assert q.status()["comments_total"] == 16
    assert q.verify()
    q.prepare_incremental()
    catchup = q.job()
    assert catchup["kind"] == "catchup"
    q.stage(catchup, terminal=True)
    q.acknowledge(now=STAMP + 2)
    q.prepare_incremental()
    sweep = q.job()
    assert sweep["kind"] == "comments"
    _page_comments(q, sweep, {"data": [edited], "max_id": 0, "total_number": 16}, SETTINGS)
    assert discussion_rows(q) == fragments
    q.acknowledge(now=STAMP + 3)
    assert q.status()["comments_total"] == 16
    q.close()


def test_incremental_cycle_alternates_catchup_and_old_roots_then_becomes_idle(tmp_path):
    q = queue(tmp_path)
    seed_posts(q)
    root = comment("200", total=1)
    _page_comments(q, q.job(), {"data": [root], "max_id": 0, "total_number": 2}, SETTINGS)
    q.acknowledge(now=STAMP)
    _page_comments(
        q, q.job(), {"data": [comment("201", root="200")], "max_id": 0, "total_number": 1}, SETTINGS
    )
    q.acknowledge(now=STAMP + 1)
    assert q.verify()
    original_snapshot = q.get("snapshot_at")
    assert q.get("incremental_since") == original_snapshot
    visited = []
    for cycle in range(2):
        for _ in range(2):
            q.prepare_incremental()
            job = q.job()
            visited.append(job["kind"])
            q.stage(job, terminal=True)
            q.acknowledge(now=STAMP + 2 + cycle)
        assert q.job() is None
        status = q.status()
        assert (
            status["pending_posts"] == status["pending_comments"] == status["pending_replies"] == 0
        )
    assert visited == ["catchup", "comments", "catchup", "replies"]
    assert q.get("snapshot_at") == original_snapshot
    q.close()


async def test_repeated_native_page_with_fresh_cursor_is_stalled(tmp_path):
    q = queue(tmp_path)
    first = FakeClient(pages=[{"list": [post()], "since_id": "one"}])
    await fetch_history_page(q, SETTINGS, Credentials(), client_factory=lambda _: first)
    q.acknowledge(now=STAMP)
    repeated = FakeClient(pages=[{"list": [post()], "since_id": "two"}])
    with pytest.raises(WeiboCollectError) as caught:
        await fetch_history_page(q, SETTINGS, Credentials(), client_factory=lambda _: repeated)
    assert caught.value.code == "pagination_stalled"
    assert q.pending() is None and q.job()["cursor"] == "one"
    q.close()


async def test_runner_cancel_during_import_keeps_pending_and_manual_resume(tmp_path, monkeypatch):
    config, clock, service, calls, runner = await runner_harness(tmp_path, monkeypatch)
    service.hold = asyncio.Event()
    await runner.tick()
    await asyncio.wait_for(service.started.wait(), 2)
    status = control.collector_overview(config, "social")
    control.cancel_collect(config, "social", status["job_id"])
    await finish(runner)
    status = control.collector_overview(config, "social")
    assert status["paused"] and status["posts_total"] == 0 and status["index_pending"] == 1
    clock.now += 9999
    await runner.tick()
    assert len(calls) == 1
    control.request_collect(config, "social")
    await runner.tick()
    await finish(runner)
    assert service.imported[0][1] == service.imported[1][1]
    assert control.collector_overview(config, "social")["posts_total"] == 1
    await runner.aclose()


async def test_request_budget_inside_long_page_keeps_current_native_pending(tmp_path, monkeypatch):
    import jianshanagent.weibo_history as history
    from jianshanagent.weibo_client import RequestBudgetReached

    q = queue(tmp_path)
    calls = []
    failed = False

    async def detail(value, client):
        nonlocal failed
        native = value["id"]
        calls.append(native)
        if native == "101" and not failed:
            failed = True
            raise RequestBudgetReached
        return row(native)

    monkeypatch.setattr(history, "_row", detail)
    source_page = {"list": [post("100"), post("101"), post("102")], "since_id": "next"}
    first = FakeClient(pages=[source_page])
    candidate = await fetch_history_page(q, SETTINGS, Credentials(), client_factory=lambda _: first)
    assert [r["id"] for r in candidate["post_rows"]] == ["100"]
    assert not candidate["advance_page"]
    q.acknowledge(now=STAMP)
    assert q.job()["page"] == 1 and q.job()["cursor"] == ""
    resumed = FakeClient(pages=[source_page])
    candidate = await fetch_history_page(
        q, SETTINGS, Credentials(), client_factory=lambda _: resumed
    )
    assert [r["id"] for r in candidate["post_rows"]] == ["101", "102"]
    assert candidate["advance_page"]
    q.acknowledge(now=STAMP + 1)
    assert calls == ["100", "101", "101", "102"]
    assert q.status()["posts_total"] == 3
    q.close()


def test_multiple_logical_publications_restart_skips_only_successful_groups(tmp_path):
    q = queue(tmp_path)
    seed_posts(q)
    _page_comments(
        q,
        q.job(),
        {"data": [comment(str(200 + i)) for i in range(70)], "max_id": 99, "total_number": 70},
        SETTINGS,
    )
    groups = list(q.publications())
    assert len(groups) == 5
    q.publication_succeeded()
    assert q.job()["cursor"] == ""
    q.close()
    q = queue(tmp_path)
    assert list(q.publications()) == groups[1:]
    assert q.status()["comments_total"] == 0
    for _ in range(4):
        q.publication_succeeded()
    assert not list(q.publications())
    q.acknowledge(now=STAMP)
    assert q.job()["cursor"] == "99" and q.status()["comments_total"] == 70
    q.close()


@pytest.mark.parametrize("field", ["longTextContent", "text_long", "longTextContent_raw"])
def test_comment_explicit_full_text_outranks_preview(field):
    value = comment(text="短预览")
    value["isLongText"] = True
    value[field] = "可靠完整正文" * 200
    assert discussion_node(value, "100")["text"] == "可靠完整正文" * 200


def test_explicit_truncation_without_complete_text_cannot_publish():
    value = comment(text="截断预览")
    value["isLongText"] = True
    with pytest.raises(WeiboCollectError) as caught:
        discussion_node(value, "100")
    assert caught.value.code == "detail_unavailable"
    value.pop("isLongText")
    assert discussion_node(value, "100")["text"] == "截断预览"  # No explicit truncation signal.


async def test_disabled_manual_history_continues_across_restart_then_no_incremental(
    tmp_path, monkeypatch
):
    config, clock, service, calls, runner = await runner_harness(tmp_path, monkeypatch)
    config.files["core.sources"]["sources"][0]["collector"]["enabled"] = False
    await runner.tick()
    assert not runner.tasks and not calls  # Saving a disabled target does not start it.
    control.request_collect(config, "social")
    await runner.tick()
    await finish(runner)
    first = control.collector_overview(config, "social")
    assert first["posts_total"] == 1 and not first["history_complete"]
    assert first["next_batch_at"] == clock.now + 2
    await runner.aclose()
    restarted = SourceCollectorRunner(
        lambda: config, clock=lambda: clock.now, service_factory=runner.service_factory
    )
    clock.now += 2
    await restarted.tick()
    await finish(restarted)
    final = control.collector_overview(config, "social")
    assert final["history_complete"] and final["next_batch_at"] is None
    assert len(calls) == 2
    clock.now += 3600
    await restarted.tick()
    assert not restarted.tasks and len(calls) == 2
    await restarted.aclose()


async def test_runner_real_v2_pipeline_posts_before_discussions_and_inline_noop(
    tmp_path, monkeypatch
):
    import jianshanagent.weibo_history as history
    from jianshanagent.plugins.knowledge.source_import import SourceImportService
    from jianshanagent.plugins.knowledge.source_import_v2 import iter_source_rows
    from jianshanagent.plugins.knowledge.store import MemoryKnowledgeStore
    from test_source_import_v2 import Embed, resource_probe

    config = full_config(tmp_path)
    config.files["core.sources"]["sources"][0]["role"] = "official"
    store, embed = MemoryKnowledgeStore(), Embed()
    clock = SimpleNamespace(now=STAMP)
    root = comment("200", total=2)
    root["comments"] = [comment("201", root="200")]
    backend = FakeClient(
        pages=[{"list": [post()], "since_id": "next"}, {"list": []}],
        envelopes=[
            {"data": [root], "max_id": 0, "total_number": 3},
            {
                "data": [comment("201", root="200")],
                "max_id": 99,
                "total_number": 2,
                "rootComment": [],
            },
            {
                "data": [comment("202", root="200")],
                "max_id": 0,
                "total_number": 2,
                "rootComment": [],
            },
        ],
    )
    original_fetch = history.fetch_history_page

    async def fetch(q, settings, credentials):
        return await original_fetch(q, settings, Credentials(), client_factory=lambda _: backend)

    async def factory(config):
        async def close():
            pass

        return SourceImportService(config, store, embed, resource_probe=resource_probe), close

    monkeypatch.setattr(history, "fetch_history_page", fetch)
    monkeypatch.setattr(history.shutil, "disk_usage", lambda _: SimpleNamespace(free=10 * 1024**3))
    runner = SourceCollectorRunner(lambda: config, clock=lambda: clock.now, service_factory=factory)
    embeddings = []
    for _ in range(5):
        await runner.tick()
        await finish(runner)
        status = control.collector_overview(config, "social")
        assert status["state"] == "completed", status
        embeddings.append(embed.new)
        clock.now += 2
    assert status["history_complete"] and status["history_phase"] == "incremental"
    assert status["posts_total"] == status["comments_total"] == 1
    assert status["replies_total"] == 2 and status["count_mismatches"] == 0
    assert (
        embeddings[2] == embeddings[3]
    )  # Inline reply repeated by dedicated page is zero-embedding.
    assert len(list(iter_source_rows(config, "social"))) == 1
    packets = list(iter_source_rows(config, "social_comments"))
    assert len(packets) == 1 and packets[0]["packet_id"] == "100:200"
    assert {node["id"] for node in packets[0]["nodes"]} == {"200", "201", "202"}
    assert {chunk.kind for chunk in store._chunks.values() if chunk.wiki == "social_comments"} == {
        "discussion"
    }
    assert all(
        chunk.meta["source_role"] == "community"
        for chunk in store._chunks.values()
        if chunk.wiki == "social_comments"
    )
    await runner.aclose()


async def test_resource_measurement_failure_blocks_before_any_http(tmp_path, monkeypatch):
    config, clock, service, calls, runner = await runner_harness(tmp_path, monkeypatch)

    async def unavailable(ids):
        raise RuntimeError("PRIVATE_NETWORK_DETAILS")

    service.resource_probe = unavailable
    await runner.tick()
    await finish(runner)
    status = control.collector_overview(config, "social")
    assert status["state"] == "blocked" and status["error_code"] == "capacity_resource_measurement"
    assert not calls and status["posts_total"] == 0 and status["pending_posts"] == 1
    assert "PRIVATE" not in json.dumps(status)
    await runner.aclose()
