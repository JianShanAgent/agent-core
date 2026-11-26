"""来源后台的隔离 HTTP 回归：不装配 Wiki、不出网、不连接数据库。"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
import yaml
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from jianshanagent import weibo_auth
from jianshanagent.admin import routes_data, routes_sources, routes_sources_session, routes_wikis
from jianshanagent.admin.auth import COOKIE, Auth
from jianshanagent.admin.core import Admin, AdminSettings, guard
from jianshanagent.core.config import load_config
from jianshanagent.plugins.knowledge.source_import import SourceImportService
from jianshanagent.plugins.knowledge.store import MemoryKnowledgeStore, StoreInfo
from test_source_import import FakeEmbedder, conf

WRITE = {"X-Requested-With": "jianshanagent-admin"}
ROW = json.dumps(
    {"version": 1, "id": "123456789", "text": "隔离测试资料", "author_name": "测试作者"}
)


class LocalAdmin(Admin):
    def status(self) -> dict[str, Any]:
        return {"config": {}}


class LocalEnv:
    def __init__(self, root: Path, overrides: Path, admin: Admin, client: TestClient):
        self.root, self.overrides, self.admin, self.client = root, overrides, admin, client
        self.reject_reload = False

    async def send(self, method: str, path: str, body: Any = None, status: int = 200) -> Any:
        response = await self.client.request(method, path, json=body, headers=WRITE)
        assert response.status == status, await response.text()
        return await response.json()

    async def create(self, source_id: str = "weibo_a") -> dict[str, Any]:
        return await self.send(
            "POST",
            "/api/sources",
            {
                "id": source_id,
                "platform": "weibo",
                "label": "测试微博",
                "role": "unknown",
            },
            201,
        )


@pytest.fixture
async def local(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[LocalEnv]:
    root, overrides = tmp_path / "config", tmp_path / "overrides"
    root.mkdir()
    (root / "core.wiki.yaml").write_text("wikis: []\n", encoding="utf-8")
    (root / "core.sources.yaml").write_text("sources: []\n", encoding="utf-8")
    (root / "plugin.knowledge.yaml").write_text(
        yaml.safe_dump({"paths": {"root": str(tmp_path / "kb")}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("JIANSHANAGENT_KB_DIR", str(tmp_path / "kb"))
    monkeypatch.delenv("JIANSHANAGENT_POSTGRES_DSN", raising=False)
    runtime = SimpleNamespace(
        config=load_config(root, overrides), db=None, host=SimpleNamespace(services={})
    )
    admin = LocalAdmin(runtime, AdminSettings(), Auth("isolated-test-token-000000"), "test")
    app = web.Application(client_max_size=20 * 1024 * 1024)
    app.middlewares.append(guard(admin))
    for module in (routes_sources, routes_wikis, routes_data):
        app.add_routes(module.routes(admin))

    async def config_change(_request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    app.router.add_post("/api/system/reload", config_change)
    app.router.add_put("/api/config", config_change)
    app.on_cleanup.append(routes_sources.imports(admin).close)
    client = TestClient(TestServer(app))
    await client.start_server()
    client.session.cookie_jar.update_cookies({COOKIE: admin.auth.issue()})
    result = LocalEnv(root, overrides, admin, client)

    async def reload() -> None:
        if result.reject_reload:
            raise RuntimeError("provider secret=do-not-expose")
        runtime.config = load_config(root, overrides)

    runtime.reload = reload
    try:
        yield result
    finally:
        await client.close()


async def test_two_sources_create_edit_stop_restore_and_legacy_wikis(local: LocalEnv) -> None:
    assert not local.admin.config.wikis
    assert (await local.send("GET", "/api/sources"))["sources"] == []
    first = (await local.create())["source"]
    await local.create("weibo_b")
    assert first["platform"] == "weibo" and first["driver"] == "documents"
    assert first["collector_status"] == "not_configured"
    assert first["knowledge"]["chunks"] == 0
    await local.send("PUT", "/api/sources/weibo_a", {"label": "我的来源", "role": "community"})
    await local.send("POST", "/api/sources/weibo_a/mount", {"mounted": False})
    listed = (await local.send("GET", "/api/sources"))["sources"]
    assert [item["id"] for item in listed] == ["weibo_a", "weibo_b"]
    assert listed[0]["label"] == "我的来源" and listed[0]["role"] == "community"
    assert listed[0]["mounted"] is False and listed[1]["mounted"] is True
    await local.send("POST", "/api/sources/weibo_a/mount", {"mounted": True})
    wikis = await local.send("GET", "/api/wikis")
    assert wikis["wikis"] == []
    assert "weibo" not in {item["id"] for item in wikis["platforms"]}
    assert "weibo" not in {item["platform"] for item in wikis["sites"]}
    knowledge = await local.send("GET", "/api/kb/summary")
    assert knowledge["wikis"] == [] and len(knowledge["sources"]) == 2


@pytest.mark.parametrize(
    "body",
    [
        {"id": "../escape", "platform": "weibo"},
        {"id": "safe", "platform": "mediawiki"},
        {"id": "safe", "platform": "weibo", "path": "/tmp/secret"},
        {"id": "safe", "platform": "weibo", "package": "arbitrary.module"},
        {"id": "safe", "platform": "weibo", "api_url": "http://localhost/"},
        {"id": "safe", "platform": "weibo", "role": ["official"]},
        {"id": "safe", "platform": "weibo", "mounted": "true"},
    ],
)
async def test_strict_source_payload(local: LocalEnv, body: dict[str, Any]) -> None:
    await local.send("POST", "/api/sources", body, 400)


async def test_collision_immutable_identity_and_failed_reload_restore(local: LocalEnv) -> None:
    (local.root / "core.wiki.yaml").write_text(
        "wikis:\n  - id: legacy\n    api_url: https://example.invalid/api.php\n",
        encoding="utf-8",
    )
    await local.admin.runtime.reload()
    await local.send("POST", "/api/sources", {"id": "legacy", "platform": "weibo"}, 409)
    await local.create()
    await local.send("PUT", "/api/sources/weibo_a", {"id": "other"}, 400)
    await local.send("PUT", "/api/sources/weibo_a", {"platform": "mediawiki"}, 400)
    path = local.overrides / "core.sources.yaml"
    before = path.read_bytes()
    local.reject_reload = True
    failed = await local.send("PUT", "/api/sources/weibo_a", {"label": "不应保存"}, 409)
    assert path.read_bytes() == before
    assert "do-not-expose" not in json.dumps(failed)
    assert (await local.send("GET", "/api/sources"))["sources"][1]["label"] == "测试微博"


async def test_auth_csrf_preview_limits_and_forged_ownership(local: LocalEnv) -> None:
    await local.create()
    response = await local.client.post("/api/sources", json={"id": "evil", "platform": "weibo"})
    assert response.status == 403
    preview = await local.send("POST", "/api/sources/weibo_a/preview", {"content": ROW})
    assert preview["source_id"] == "weibo_a" and preview["records"] == 1
    assert preview["sample"][0]["author_name"] == "测试作者"
    for content in [
        "not-json",
        ROW.replace('"version": 1', '"source_id": "weibo_b", "version": 1'),
    ]:
        await local.send("POST", "/api/sources/weibo_a/preview", {"content": content}, 400)
    await local.send("POST", "/api/sources/weibo_a/preview", {"url": "http://localhost/"}, 400)
    await local.send(
        "POST", "/api/sources/weibo_a/preview", {"content": "x" * (10 * 1024 * 1024 + 1)}, 413
    )
    await local.send("POST", "/api/sources/weibo_a/import", {"content": ROW}, 503)
    local.client.session.cookie_jar.clear()
    assert (await local.client.get("/api/sources")).status == 401
    assert (
        await local.client.post("/api/sources/weibo_a/import", json={"content": ROW}, headers=WRITE)
    ).status == 401


async def test_document_sources_reject_wiki_operations(local: LocalEnv) -> None:
    await local.create()
    for method, path, body, expected in [
        ("PUT", "/api/wikis/weibo_a/authkey", {"authkey": "secret"}, 404),
        ("POST", "/api/wikis/weibo_a/kb", {}, 404),
        ("DELETE", "/api/wikis/weibo_a/kb", {}, 404),
        ("PUT", "/api/wikis/weibo_a/mounted", {"mounted": False}, 404),
        ("POST", "/api/wikis/bilibili/qr", {}, 409),
        ("POST", "/api/wikis/bilibili/poll", {}, 409),
        ("GET", "/api/kb/images?source_id=weibo_a", None, 409),
    ]:
        await local.send(method, path, body, expected)


class PendingImport:
    def __init__(self, config: Any, store: Any):
        self.config, self.store = config, store
        self.states: dict[str, dict[str, Any]] = {}
        self.release = asyncio.Event()
        self.started = asyncio.Event()

    def preview(self, source_id: str, _content: str) -> dict[str, Any]:
        return {
            "source_id": source_id,
            "records": 1,
            "new": 1,
            "updated": 0,
            "unchanged": 0,
            "total_documents": 1,
            "chunks": 1,
            "empty": False,
            "sample": [],
        }

    async def import_data(self, source_id: str, _content: str) -> dict[str, Any]:
        self.states[source_id] = {
            "source_id": source_id,
            "state": "embedding",
            "job_id": "test-job",
        }
        self.started.set()
        await self.release.wait()
        self.states[source_id] = {
            "source_id": source_id,
            "state": "cancelled",
            "job_id": "test-job",
        }
        return self.states[source_id]

    def status(self, source_id: str) -> dict[str, Any] | None:
        return self.states.get(source_id)

    def cancel(self, source_id: str) -> bool:
        if source_id not in self.states:
            return False
        self.release.set()
        return True

    async def aclose(self) -> None:
        self.release.set()


async def test_import_task_mutex_status_cancel_and_reload_handle(
    local: LocalEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await local.create()
    await local.create("weibo_b")
    manager = routes_sources.imports(local.admin)
    fake = PendingImport(local.admin.config, MemoryKnowledgeStore())
    manager.service = fake
    monkeypatch.setattr(Admin, "has_database", property(lambda self: True))
    await local.send("POST", "/api/sources/weibo_a/import", {"content": ROW}, 202)
    await fake.started.wait()
    await local.send("POST", "/api/sources/weibo_a/import", {"content": ROW}, 409)
    await local.send("PUT", "/api/sources/weibo_a", {"role": "official"}, 409)
    await local.send("POST", "/api/system/reload", {}, 409)
    await local.send("PUT", "/api/config", {}, 409)
    await local.send("POST", "/api/sources/weibo_b/mount", {"mounted": False}, 409)
    status = (await local.send("GET", "/api/sources/weibo_a/status"))["import_status"]
    assert status["state"] == "embedding" and status["job_id"] == "test-job"
    # An outside reload cannot discard Admin's service/task/cancel handle.
    await local.admin.runtime.reload()
    assert routes_sources.imports(local.admin) is manager and manager.service is fake
    assert (await local.send("POST", "/api/sources/weibo_a/cancel", {}))["cancelled"]
    await asyncio.gather(*manager.tasks.values())
    assert (await local.send("GET", "/api/sources/weibo_a/status"))["import_status"][
        "state"
    ] == "cancelled"
    await local.send("POST", "/api/sources/weibo_b/mount", {"mounted": False})


@pytest.mark.parametrize("operation", ["preview", "import"])
async def test_large_preview_runs_off_event_loop_and_import_registration_stays_locked(
    local: LocalEnv,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    await local.create()
    entered, release = threading.Event(), threading.Event()

    class SlowPreview(PendingImport):
        def preview(self, source_id: str, content: str) -> dict[str, Any]:
            entered.set()
            assert release.wait(5), "test preview release timed out"
            return super().preview(source_id, content)

    manager = routes_sources.imports(local.admin)
    manager.service = SlowPreview(local.admin.config, MemoryKnowledgeStore())
    monkeypatch.setattr(Admin, "has_database", property(lambda self: True))
    started = time.monotonic()
    pending = asyncio.create_task(
        local.send(
            "POST",
            f"/api/sources/weibo_a/{operation}",
            {"content": ROW},
            202 if operation == "import" else 200,
        )
    )
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        assert time.monotonic() - started < 1.5
        # Even while the preview worker waits, unrelated HTTP status remains responsive.
        await asyncio.wait_for(local.send("GET", "/api/sources/weibo_a/status"), 1)
        if operation == "import":
            config_change = asyncio.create_task(local.send("POST", "/api/system/reload", {}, 409))
            await asyncio.sleep(0)
            assert not config_change.done()
        release.set()
        await pending
        if operation == "import":
            await config_change
            manager.service.release.set()
            await manager.tasks["weibo_a"]
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)


async def test_capacity_413_and_controlled_async_status_message(
    local: LocalEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await local.create()
    code = next(iter(routes_sources.CAPACITY_ERROR_CODES))

    class CapacityError(ValueError):
        public_message = routes_sources.CAPACITY_PUBLIC_MESSAGE

        def __init__(self) -> None:
            self.code = code
            super().__init__("provider-key=do-not-expose")

    class CapacityImport(PendingImport):
        fail_preview = True

        def preview(self, source_id: str, content: str) -> dict[str, Any]:
            if self.fail_preview:
                raise CapacityError()
            return super().preview(source_id, content)

        async def import_data(self, source_id: str, content: str) -> dict[str, Any]:
            raise CapacityError()

    manager = routes_sources.imports(local.admin)
    service = CapacityImport(local.admin.config, MemoryKnowledgeStore())
    manager.service = service
    monkeypatch.setattr(Admin, "has_database", property(lambda self: True))
    monkeypatch.setattr(routes_sources, "SourceImportCapacityError", CapacityError)
    for operation in ("preview", "import"):
        response = await local.send(
            "POST", f"/api/sources/weibo_a/{operation}", {"content": ROW}, 413
        )
        assert response["error"] == routes_sources.CAPACITY_PUBLIC_MESSAGE
        assert "do-not-expose" not in json.dumps(response)
    service.fail_preview = False
    await local.send("POST", "/api/sources/weibo_a/import", {"content": ROW}, 202)
    await manager.tasks["weibo_a"]
    status = (await local.send("GET", "/api/sources/weibo_a/status"))["import_status"]
    assert status["state"] == "failed" and status["error_code"] == code
    assert status["error"] == routes_sources.CAPACITY_PUBLIC_MESSAGE
    # Persisted controlled codes keep the same safe message; arbitrary error text is never used.
    manager.results.clear()
    service.states["weibo_a"] = {"state": "failed", "error_code": code, "error": "token=secret"}
    assert manager.status("weibo_a")["public_message"] == routes_sources.CAPACITY_PUBLIC_MESSAGE
    service.states["weibo_a"]["error_code"] = "provider-key=do-not-expose"
    assert "do-not-expose" not in json.dumps(manager.status("weibo_a"))
    assert "error_code" not in manager.status("weibo_a")


async def test_retained_counts_and_status_privacy(local: LocalEnv) -> None:
    await local.create()
    info = StoreInfo(
        chunks=7,
        with_vectors=7,
        fingerprint="secret-fingerprint",
        wikis={"weibo_a": StoreInfo(chunks=7, with_vectors=7, loaded_at="2026-01-01")},
    )

    class Store:
        async def info(self) -> StoreInfo:
            return info

    local.admin.runtime.host.services["knowledge"] = SimpleNamespace(store=Store())
    manager = routes_sources.imports(local.admin)
    manager.results["weibo_a"] = {
        "source_id": "weibo_a",
        "state": "failed",
        "error": "api_key=do-not-expose",
        "fingerprint": "secret",
    }
    await local.send("POST", "/api/sources/weibo_a/mount", {"mounted": False})
    listed = await local.send("GET", "/api/sources")
    source = listed["sources"][0]
    assert source["mounted"] is False and source["knowledge"]["chunks"] == 7
    assert "do-not-expose" not in json.dumps(listed) and "fingerprint" not in json.dumps(listed)
    assert source["import_status"]["state"] == "failed"


async def test_kb_source_labels_and_safe_provenance(
    local: LocalEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await local.create()
    row = {
        "wiki": "weibo_a",
        "chunk_id": "weibo_a:post:1",
        "fingerprint": "private-fingerprint",
        "url": "https://user:password@example.invalid/post",
        "meta": {
            "post_id": "123456789",
            "canonical_document_id": "weibo:123456789",
            "author_name": "测试作者",
            "api_key": "do-not-expose",
            "repost": {"id": "987654321", "url": "javascript:alert(1)", "token": "private"},
        },
    }

    class Queries:
        async def kb_chunks(self, **kwargs: Any) -> dict[str, Any]:
            assert kwargs["wiki"] == "weibo_a"
            return {"total": 1, "items": [dict(row)]}

        async def kb_chunk(self, _id: str) -> dict[str, Any]:
            return dict(row)

    monkeypatch.setattr(LocalAdmin, "queries", property(lambda self: Queries()))
    detail = await local.send("GET", "/api/kb/chunks/weibo_a:post:1")
    assert detail["source_id"] == "weibo_a" and detail["source_label"] == "测试微博"
    assert detail["meta"]["post_id"] == "123456789"
    assert detail["url"] == "" and detail["meta"]["repost"]["url"] == ""
    assert "api_key" not in detail["meta"] and "token" not in detail["meta"]["repost"]
    assert "fingerprint" not in detail
    listed = await local.send("GET", "/api/kb/chunks?source_id=weibo_a")
    assert listed["items"][0]["source_label"] == "测试微博"


def test_provenance_whitelist() -> None:
    assert routes_sources.safe_provenance({"author_name": "测试", "api_key": "secret"}) == {
        "author_name": "测试"
    }
    assert routes_sources.safe_url("javascript:alert(1)") == ""
    assert routes_sources.safe_url("https://user:password@example.invalid/") == ""


async def test_http_real_import_service_two_namespaces_and_restore(
    local: LocalEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await local.create()
    await local.create("weibo_b")
    (local.root / "core.embedding.yaml").write_text(
        yaml.safe_dump(conf(local.root.parent).files["core.embedding"]),
        encoding="utf-8",
    )
    await local.admin.runtime.reload()
    store, embedder = MemoryKnowledgeStore(), FakeEmbedder()
    manager = routes_sources.imports(local.admin)
    manager.service = SourceImportService(local.admin.config, store, embedder)
    local.admin.runtime.host.services["knowledge"] = SimpleNamespace(store=store)
    monkeypatch.setattr(Admin, "has_database", property(lambda self: True))
    for source_id in ("weibo_a", "weibo_b"):
        preview = await local.send("POST", f"/api/sources/{source_id}/preview", {"content": ROW})
        assert preview["records"] == 1 and embedder.calls == (0 if source_id == "weibo_a" else 1)
        await local.send("POST", f"/api/sources/{source_id}/import", {"content": ROW}, 202)
        await manager.tasks[source_id]
        status = (await local.send("GET", f"/api/sources/{source_id}/status"))["import_status"]
        assert status["state"] == "completed"
    assert len(store._chunks) == 2 and {item.wiki for item in store._chunks.values()} == {
        "weibo_a",
        "weibo_b",
    }
    assert embedder.calls == 2
    await local.send("POST", "/api/sources/weibo_a/import", {"content": ROW}, 202)
    await manager.tasks["weibo_a"]
    assert embedder.calls == 2  # Idempotent imports do not re-embed.
    before = dict(store._chunks)
    await local.send("POST", "/api/sources/weibo_a/mount", {"mounted": False})
    assert store._chunks == before
    listed = (await local.send("GET", "/api/sources"))["sources"]
    assert listed[0]["knowledge"]["chunks"] == 1 and listed[0]["mounted"] is False
    await local.send("POST", "/api/sources/weibo_a/import", {"content": ROW}, 409)
    await local.send("POST", "/api/sources/weibo_a/mount", {"mounted": True})
    assert store._chunks == before
    # A fresh Admin manager reads persisted terminal status without losing job ownership.
    fresh = routes_sources.SourceImports(local.admin)
    assert fresh.status("weibo_a")["state"] == "completed"
    directory = manager.service._directory("weibo_a")
    generation = json.loads((directory / "current.json").read_text("utf-8"))["generation"]
    snapshot = json.loads(
        (directory / "generations" / generation / "snapshot.json").read_text("utf-8")
    )
    (directory / "journal.json").write_text(
        json.dumps(
            {
                "previous": generation,
                "candidate": "unpublished",
                "fingerprint": snapshot["fingerprint"],
            }
        ),
        encoding="utf-8",
    )
    assert manager.status("weibo_a")["state"] == "recovery_required"
    await local.send("POST", "/api/sources/weibo_a/preview", {"content": ROW}, 400)
    # Explicit empty import restores the full saved snapshot before accepting another batch.
    await local.send("POST", "/api/sources/weibo_a/import", {"content": ""}, 202)
    await manager.tasks["weibo_a"]
    assert manager.status("weibo_a")["state"] == "completed"
    assert store._chunks == before and not (directory / "journal.json").exists()


class FakeSessionStore:
    def __init__(self, _root: Path):
        self.configured = False

    def status(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "locally_usable": self.configured,
            "valid": None,
            "updated_at": 123 if self.configured else None,
            "cookie": "never-expose-this-cookie",
        }

    def clear(self) -> None:
        self.configured = False


class FakeLogin:
    instances: list[Any] = []

    def __init__(self, store: FakeSessionStore):
        self.store, self.state, self.error, self.available = store, "new", "", True
        self.login_id, self.method, self.verified = uuid4().hex, "http_wechat", False
        self.closed = False
        self.instances.append(self)

    async def generate(self) -> str:
        self.state = "waiting"
        return "data:image/png;base64,aGVsbG8="

    async def poll(self) -> str:
        self.state = "confirmed"
        self.store.configured = True
        return self.state

    async def aclose(self) -> None:
        self.closed = True
        if self.state != "confirmed":
            self.state = "cancelled"


@pytest.fixture
def fake_login(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeLogin.instances.clear()
    monkeypatch.setattr(routes_sources_session, "WeiboCredentialStore", FakeSessionStore)
    monkeypatch.setattr(routes_sources_session, "WeiboQrLogin", FakeLogin)
    monkeypatch.setattr(
        routes_sources_session,
        "weibo_login_status",
        lambda: {"available": True, "method": "http_wechat", "message": "测试 HTTP 组件"},
    )


async def test_shared_weibo_session_login_clear_and_privacy(
    local: LocalEnv, fake_login: None
) -> None:
    prefix = "/api/sources/platforms/weibo"
    await local.send("GET", prefix + "/session", status=409)
    await local.create()
    await local.create("weibo_b")
    status = await local.send("GET", prefix + "/session")
    assert status["configured"] is False and status["valid"] is None
    assert FakeLogin.instances == []  # Status must not start a browser.
    bare = await local.client.post(prefix + "/qr", json={})
    assert bare.status == 403
    await local.send("POST", prefix + "/qr", {"cdp_url": "http://localhost/"}, 400)
    qr = await local.send("POST", prefix + "/qr", {})
    assert qr["image"].startswith("data:image/png;")
    await local.send("POST", prefix + "/qr", {}, 409)
    assert (await local.send("GET", prefix + "/poll?login_id=" + qr["login_id"]))[
        "state"
    ] == "confirmed"
    status = await local.send("GET", prefix + "/session")
    assert status["configured"] is True and status["valid"] is None
    assert "never-expose" not in json.dumps(status)
    assert len(FakeLogin.instances) == 1
    sources = (await local.send("GET", "/api/sources"))["sources"]
    assert all(source["role"] == "unknown" for source in sources)
    assert (await local.send("DELETE", prefix + "/session", {}))["configured"] is False
    assert FakeLogin.instances[0].closed
    qr = await local.send("POST", prefix + "/qr", {})
    assert (await local.send("POST", prefix + "/cancel", {"login_id": qr["login_id"]}))[
        "state"
    ] == "cancelled"
    assert FakeLogin.instances[-1].closed


async def test_weibo_session_without_browser_and_cleanup(
    local: LocalEnv,
    fake_login: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await local.create()
    prefix = "/api/sources/platforms/weibo"
    monkeypatch.setattr(
        weibo_auth,
        "weibo_browser_status",
        lambda: {"available": False, "message": "未发现本机浏览器"},
    )
    assert (await local.send("GET", prefix + "/session"))["available"] is True
    qr = await local.send("POST", prefix + "/qr", {})
    assert qr["method"] == "http_wechat"
    assert len(FakeLogin.instances) == 1
    await local.client.close()
    assert FakeLogin.instances[-1].closed


async def test_external_reload_during_import_preserves_old_ownership(
    local: LocalEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await local.create()
    (local.root / "core.embedding.yaml").write_text(
        yaml.safe_dump(conf(local.root.parent).files["core.embedding"]),
        encoding="utf-8",
    )
    await local.admin.runtime.reload()
    store, embedder = MemoryKnowledgeStore(), FakeEmbedder()
    manager = routes_sources.imports(local.admin)
    manager.service = SourceImportService(local.admin.config, store, embedder)
    monkeypatch.setattr(Admin, "has_database", property(lambda self: True))
    await local.send("POST", "/api/sources/weibo_a/import", {"content": ROW}, 202)
    await manager.tasks["weibo_a"]
    original = dict(store._chunks)
    embedder.started.clear()
    embedder.release = asyncio.Event()
    updated = ROW.replace("123456789", "987654321")
    await local.send("POST", "/api/sources/weibo_a/import", {"content": updated}, 202)
    await embedder.started.wait()
    # An operator edits a config file then calls Runtime.reload directly, outside Admin.guard.
    config_file = local.overrides / "core.sources.yaml"
    config_file.write_text(
        config_file.read_text("utf-8").replace("unknown", "community"), encoding="utf-8"
    )
    await local.admin.runtime.reload()
    embedder.release.set()
    await manager.tasks["weibo_a"]
    assert store._chunks == original
    status = (await local.send("GET", "/api/sources/weibo_a/status"))["import_status"]
    assert status["state"] == "failed"
