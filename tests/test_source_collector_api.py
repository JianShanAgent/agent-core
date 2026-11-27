"""采集控制真实文件队列/lease 的 HTTP 契约；不出网、不跑采集。"""

from __future__ import annotations

import asyncio
import json
import multiprocessing
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from jianshanagent.admin import routes_sources
from jianshanagent.admin.core import Admin
from jianshanagent.core.config import load_config
from jianshanagent.plugins.knowledge.source_collect import acquire_source_operation
from test_source_import import FakeEmbedder, conf
from test_sources_admin import ROW, WRITE, LocalEnv, PendingImport
from test_sources_admin import local as local

UID = "5896401674"


async def configured(local: LocalEnv) -> dict[str, Any]:
    await local.create()
    return await local.send(
        "PUT",
        "/api/sources/weibo_a",
        {
            "role": "official",
            "collector": {"uid": f"https://weibo.com/u/{UID}"},
        },
    )


async def test_api_canonical_config_and_complete_replacement(local: LocalEnv) -> None:
    result = (await configured(local))["source"]
    assert result["role"] == "official" and result["mounted"] is True
    assert result["collector"]["uid"] == UID and result["collector"]["enabled"] is False
    assert result["collection"]["uid"] == UID
    assert result["collector_status"] == result["collection"]["state"]
    await local.send(
        "PUT",
        "/api/sources/weibo_a",
        {
            "collector": {"uid": UID, "initial_days": 100, "max_posts": 400},
        },
    )
    updated = await local.send("PUT", "/api/sources/weibo_a", {"collector": {"uid": UID}})
    assert updated["source"]["collector"]["initial_days"] == 30
    assert updated["source"]["collector"]["max_posts"] == 200
    assert updated["source"]["role"] == "official"
    for replacement in ({"uid": "1234"}, None, {}):
        await local.send(
            "PUT",
            "/api/sources/weibo_a",
            {"collector": replacement},
            400 if replacement is None or replacement == {} else 409,
        )
    assert (await local.send("GET", "/api/sources"))["sources"][0]["collector"]["uid"] == UID


@pytest.mark.parametrize(
    "collector",
    [
        {"uid": UID, "cookie": "secret"},
        {"uid": UID, "proxy": "http://localhost"},
        {"uid": "https://weibo.com.evil/u/1234"},
        {"uid": UID, "initial_days": True},
        {"uid": UID, "max_posts": 501},
        {"uid": UID, "interval_seconds": 1},
        {"uid": UID, "enabled": "false"},
        {"uid": UID, "include_reposts": 0},
    ],
)
async def test_config_rejects_unsafe_or_out_of_bounds(
    local: LocalEnv,
    collector: dict[str, Any],
) -> None:
    await local.send(
        "POST",
        "/api/sources",
        {
            "id": "weibo_a",
            "platform": "weibo",
            "collector": collector,
        },
        400,
    )


async def test_collector_config_save_reload_failure_restores(local: LocalEnv) -> None:
    await configured(local)
    before = (local.overrides / "core.sources.yaml").read_bytes()
    local.reject_reload = True
    await local.send(
        "PUT",
        "/api/sources/weibo_a",
        {
            "collector": {"uid": UID, "enabled": True},
        },
        409,
    )
    assert (local.overrides / "core.sources.yaml").read_bytes() == before
    listed = await local.send("GET", "/api/sources")
    assert listed["sources"][0]["collector"]["enabled"] is False


async def test_collect_only_queues_then_stale_cancel_and_guard(
    local: LocalEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await configured(local)
    monkeypatch.setattr(Admin, "has_database", property(lambda self: True))
    queued = await local.send("POST", "/api/sources/weibo_a/collect", {}, 202)
    assert queued["state"] == "queued" and queued["job_id"]
    assert queued["uid"] == UID and queued["pages"] == queued["accepted"] == 0
    assert not routes_sources.imports(local.admin).tasks
    status = await local.send("GET", "/api/sources/weibo_a/collector")
    assert status["job_id"] == queued["job_id"] and status["state"] == "queued"
    listed = (await local.send("GET", "/api/sources"))["sources"][0]
    assert listed["collector_status"] == "queued" and listed["collection"] == status
    await local.send("POST", "/api/sources/weibo_a/collect", {}, 409)
    await local.send("POST", "/api/sources/weibo_a/import", {"content": ROW}, 409)
    await local.send("POST", "/api/sources/weibo_a/mount", {"mounted": False}, 409)
    await local.send("PUT", "/api/sources/weibo_a", {"collector": {"uid": UID}}, 409)
    await local.send("POST", "/api/system/reload", {}, 409)
    await local.send("POST", "/api/sources/weibo_a/collect/cancel", {"job_id": "stale"}, 409)
    cancelled = await local.send(
        "POST",
        "/api/sources/weibo_a/collect/cancel",
        {
            "job_id": queued["job_id"],
        },
    )
    assert cancelled["state"] == "cancelled"
    again = await local.send("POST", "/api/sources/weibo_a/collect", {}, 202)
    assert again["job_id"] != queued["job_id"]
    await local.send(
        "POST",
        "/api/sources/weibo_a/collect/cancel",
        {
            "job_id": queued["job_id"],
        },
        409,
    )
    await local.send("POST", "/api/sources/weibo_a/collect/cancel", {"job_id": again["job_id"]})
    await local.send("POST", "/api/sources/weibo_a/mount", {"mounted": False})
    await local.send("POST", "/api/sources/weibo_a/collect", {}, 409)


async def test_collect_auth_csrf_payload_and_missing_target(
    local: LocalEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await local.create()
    status = await local.send("GET", "/api/sources/weibo_a/collector")
    assert status["state"] == "not_configured"
    await local.send("POST", "/api/sources/weibo_a/collect", {}, 409)
    await local.send("POST", "/api/sources/missing/collect", {}, 404)
    await local.send("PUT", "/api/sources/weibo_a", {"collector": {"uid": UID}})
    await local.send("POST", "/api/sources/weibo_a/collect", {}, 503)
    monkeypatch.setattr(Admin, "has_database", property(lambda self: True))
    for body in ({"uid": UID}, {"cookie": "secret"}, [], None):
        await local.send("POST", "/api/sources/weibo_a/collect", body, 400)
    for body in ({}, {"job_id": "x", "cookie": "secret"}, {"job_id": 7}):
        await local.send("POST", "/api/sources/weibo_a/collect/cancel", body, 400)
    assert (await local.client.post("/api/sources/weibo_a/collect", json={})).status == 403
    local.client.session.cookie_jar.clear()
    assert (await local.client.get("/api/sources/weibo_a/collector")).status == 401
    assert (
        await local.client.post("/api/sources/weibo_a/collect", json={}, headers=WRITE)
    ).status == 401


def _hold_operation(root: Path, overrides: Path, ready: Any, release: Any) -> None:
    with acquire_source_operation(load_config(root, overrides), "weibo_a"):
        ready.set()
        release.wait(10)


async def test_other_process_operation_blocks_import_config_and_reload(
    local: LocalEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await configured(local)
    monkeypatch.setattr(Admin, "has_database", property(lambda self: True))
    context = multiprocessing.get_context("spawn")
    ready, release = context.Event(), context.Event()
    child = context.Process(
        target=_hold_operation, args=(local.root, local.overrides, ready, release)
    )
    child.start()
    try:
        assert await asyncio.to_thread(ready.wait, 5)
        assert routes_sources.imports(local.admin).tasks == {}
        await local.send("POST", "/api/sources/weibo_a/collect", {}, 409)
        await local.send("POST", "/api/sources/weibo_a/import", {"content": ROW}, 409)
        await local.send("PUT", "/api/sources/weibo_a", {"role": "community"}, 409)
        await local.send("POST", "/api/system/reload", {}, 409)
    finally:
        release.set()
        await asyncio.to_thread(child.join, 5)
        if child.is_alive():
            child.terminate()
            child.join()
    assert child.exitcode == 0
    await local.send("PUT", "/api/sources/weibo_a", {"label": "释放后可保存"})


async def test_manual_import_holds_operation_until_task_finally(
    local: LocalEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await local.create()
    manager = routes_sources.imports(local.admin)
    service = PendingImport(local.admin.config, object())
    manager.service = service
    monkeypatch.setattr(Admin, "has_database", property(lambda self: True))
    await local.send("POST", "/api/sources/weibo_a/import", {"content": ROW}, 202)
    await service.started.wait()
    from jianshanagent.plugins.knowledge.source_collect import SourceCollectError

    with pytest.raises(SourceCollectError):
        acquire_source_operation(local.admin.config, "weibo_a")
    await local.send("POST", "/api/sources/weibo_a/cancel", {})
    await manager.tasks["weibo_a"]
    with acquire_source_operation(local.admin.config, "weibo_a"):
        pass


async def test_persisted_collection_errors_are_not_public_raw_messages(local: LocalEnv) -> None:
    await configured(local)
    from jianshanagent.plugins.knowledge.settings import kb_root

    directory = kb_root(local.admin.config) / "weibo_a" / "source-collect"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "status.json").write_text(
        json.dumps(
            {
                "state": "failed",
                "job_id": "a" * 32,
                "error_code": "token=secret",
                "message": "Cookie secret provider-token",
                "cookie": "private",
                "signature": "private",
            }
        )
    )
    response = await local.send("GET", "/api/sources/weibo_a/collector")
    assert response["state"] == "failed" and response["error_code"] == "failed"
    assert "secret" not in json.dumps(response) and "private" not in json.dumps(response)


@pytest.mark.parametrize("author", [UID, "1234", None])
async def test_binding_checks_database_authors_before_save(
    local: LocalEnv,
    author: str | None,
) -> None:
    await local.create()

    class Pool:
        async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
            if "DISTINCT meta->>'author_id'" in query:
                assert args == ("weibo_a",)
                return [{"author_id": author}]
            return []

    local.admin.runtime.db = SimpleNamespace(pool=Pool())
    response = await local.send(
        "PUT",
        "/api/sources/weibo_a",
        {
            "collector": {"uid": UID},
            "role": "official",
        },
        200 if author == UID else 409,
    )
    if author == UID:
        assert response["source"]["collector"]["uid"] == UID
        assert response["source"]["role"] == "official"
    else:
        assert "新建来源" in response["error"]
        assert "collector" not in local.admin.disk_config().source("weibo_a").raw


@pytest.mark.parametrize("change", ["uid", "role", "mounted", "window", "directory", "embedding"])
async def test_admin_import_rechecks_disk_without_runtime_reload(
    local: LocalEnv,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    monkeypatch.delenv("JIANSHANAGENT_KB_DIR", raising=False)
    embedding_path = local.root / "core.embedding.yaml"
    embedding_path.write_text(yaml.safe_dump(conf(local.root.parent).files["core.embedding"]))
    await local.admin.runtime.reload()
    await configured(local)
    monkeypatch.setattr(Admin, "has_database", property(lambda self: True))
    manager = routes_sources.imports(local.admin)
    service = manager.get()  # Exercise the actual Admin constructor's disk-config provider.
    embedder = FakeEmbedder()
    service.embedder = embedder
    first = json.dumps({"version": 1, "id": "100", "text": "原有完整微博正文", "author_id": UID})
    await local.send("POST", "/api/sources/weibo_a/import", {"content": first}, 202)
    await manager.tasks["weibo_a"]
    assert manager.status("weibo_a")["state"] == "completed"
    before = dict(service.store._chunks)
    embedder.started.clear()
    embedder.release = asyncio.Event()
    second = json.dumps({"version": 1, "id": "101", "text": "不应提交的新正文", "author_id": UID})
    await local.send("POST", "/api/sources/weibo_a/import", {"content": second}, 202)
    await asyncio.wait_for(embedder.started.wait(), 3)
    try:
        if change in {"uid", "role", "mounted", "window"}:
            path = local.overrides / "core.sources.yaml"
            value = yaml.safe_load(path.read_text())
            source = value["sources"][0]
            if change == "uid":
                source["collector"]["uid"] = "1234"
            elif change == "window":
                source["collector"]["initial_days"] = 31
            elif change == "role":
                source["role"] = "community"
            else:
                source["mounted"] = False
            path.write_text(yaml.safe_dump(value))
        elif change == "directory":
            path = local.root / "plugin.knowledge.yaml"
            path.write_text(
                yaml.safe_dump({"paths": {"root": str(local.root.parent / "other-kb")}})
            )
        else:
            value = yaml.safe_load(embedding_path.read_text())
            value["providers"]["test"]["model"] = "changed-model"
            embedding_path.write_text(yaml.safe_dump(value))
        # Runtime's snapshot deliberately remains unchanged; only the provider can see disk edits.
        assert local.admin.config.source("weibo_a").role == "official"
    finally:
        embedder.release.set()
        await manager.tasks["weibo_a"]
    assert service.store._chunks == before
    assert manager.results["weibo_a"]["state"] == "failed"
