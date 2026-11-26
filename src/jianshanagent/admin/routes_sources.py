"""通用资料来源：只登记受信任平台、接收标准资料，不拉取 URL 或执行上传代码。"""

from __future__ import annotations

import asyncio
import re
from typing import Any, cast
from urllib.parse import urlsplit

from aiohttp import web

from jianshanagent.admin.core import Admin, fail, read_body, reply
from jianshanagent.admin.routes_sources_session import routes as session_routes
from jianshanagent.core.config import AppConfig, restore_override, write_source_instances
from jianshanagent.core.manifest import platforms
from jianshanagent.core.sources import SourceInstance, source_instance, source_instances
from jianshanagent.plugins.knowledge.source_import import (
    CAPACITY_ERROR_CODES,
    CAPACITY_PUBLIC_MESSAGE,
    SourceImportCapacityError,
    SourceImportService,
)
from jianshanagent.plugins.knowledge.store import MemoryKnowledgeStore, PostgresKnowledgeStore

_SOURCE_FILE = "core.sources"
_ID = re.compile(r"[a-z][a-z0-9_]{0,31}")
_ROLES = {"unknown", "official", "community"}
# JSON escaping can enlarge a 10 MiB UTF-8 payload; limit the entire request independently.
_MAX_BODY = 16 * 1024 * 1024
_MAX_CONTENT = 10 * 1024 * 1024
_META_FIELDS = {
    "source_platform",
    "canonical_document_id",
    "origin_id",
    "post_id",
    "author_id",
    "author_name",
    "source_role",
    "published_at",
    "edited_at",
    "collected_at",
    "collection",
    "repost",
    "origin_url",
    "mirror_url",
    "repost_of",
    "canonical_id",
}


def safe_provenance(meta: Any) -> dict[str, Any]:
    """Only reference metadata crosses this API boundary, never arbitrary adapter config."""
    if not isinstance(meta, dict):
        return {}

    def clean(value: Any, depth: int = 0) -> Any:
        if isinstance(value, str):
            return value[:4000]
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, dict) and depth < 2:
            return {
                str(key): clean(item, depth + 1)
                for key, item in value.items()
                if key in {"id", "url", "text", "author_id", "author_name", "published_at"}
            }
        return None

    result = {key: clean(value) for key, value in meta.items() if key in _META_FIELDS}
    for key in {"origin_url", "mirror_url"} & result.keys():
        result[key] = safe_url(result[key])
    if isinstance(result.get("repost"), dict) and "url" in result["repost"]:
        result["repost"]["url"] = safe_url(result["repost"]["url"])
    return result


def safe_url(value: Any) -> str:
    """Reference links cannot contain credentials or browser-executable schemes."""
    if not isinstance(value, str):
        return ""
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return ""
        if parsed.username is not None or parsed.password is not None:
            return ""
    except ValueError:
        return ""
    return value


class _CurrentConfig:
    """A live view lets the service's commit guard detect even an external Runtime.reload()."""

    def __init__(self, admin: Admin):
        self.admin = admin

    def __getattr__(self, name: str) -> Any:
        return getattr(self.admin.config, name)


class SourceImports:
    """Owned by Admin, not a reloadable plugin: task and cancellation handles survive reloads."""

    def __init__(self, admin: Admin):
        self.admin = admin
        self.service: SourceImportService | None = None
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.results: dict[str, dict[str, Any]] = {}
        self._database: Any = None
        self.config = cast(AppConfig, _CurrentConfig(admin))

    def active(self) -> bool:
        return any(not task.done() for task in self.tasks.values())

    def get(self) -> SourceImportService:
        database = self.admin.runtime.db
        if self.service is None or (database is not self._database and not self.active()):
            # These stores only wrap the runtime-owned connection pool; they own no connections.
            store = (
                PostgresKnowledgeStore(database) if database is not None else MemoryKnowledgeStore()
            )
            self.service = SourceImportService(self.config, store)
            self._database = database
        self.service.config = self.config
        return self.service

    def status(self, source_id: str) -> dict[str, Any] | None:
        try:
            service_status = self.get().status(source_id)
        except (ValueError, OSError, TypeError, AttributeError):
            service_status = {"source_id": source_id, "state": "recovery_required"}
        status = service_status or self.results.get(source_id)
        task = self.tasks.get(source_id)
        if task is not None and not task.done() and service_status is not None:
            if service_status.get("state") not in {"validating", "embedding", "committing"}:
                status = self.results.get(source_id) or service_status
        if task is not None and task.cancelled():
            status = self.results.get(source_id)
        if status is None:
            return None
        # Persisted provider failures may contain credentials. Expose only controlled state/counts.
        allowed = {
            "source_id",
            "state",
            "job_id",
            "records",
            "new",
            "updated",
            "unchanged",
            "total_documents",
            "chunks",
            "empty",
            "started_at",
            "finished_at",
            "cancel_requested",
            "updated_at",
            "retryable",
            "noop",
        }
        result = {key: value for key, value in status.items() if key in allowed}
        if result.get("state") == "recovery_required":
            result["error"] = "完整快照已保留，需恢复导入状态后再导入新资料"
        elif result.get("state") == "failed":
            result["error"] = "导入失败，请检查嵌入配置或后台日志后重试"
            code = status.get("error_code") or (self.results.get(source_id) or {}).get("error_code")
            if isinstance(code, str) and code in CAPACITY_ERROR_CODES:
                result.update(
                    error_code=code,
                    error=CAPACITY_PUBLIC_MESSAGE,
                    public_message=CAPACITY_PUBLIC_MESSAGE,
                )
        return result

    async def start(self, source_id: str, content: str) -> dict[str, Any]:
        if source_id in self.tasks and not self.tasks[source_id].done():
            fail(409, "这个来源已有导入正在运行")
        service = self.get()
        # Recovery is an explicit empty batch: replay the last complete snapshot, no new records.
        recovering = (self.status(source_id) or {}).get("state") == "recovery_required"
        if recovering and not content.strip():
            preview = {
                "source_id": source_id,
                "records": 0,
                "new": 0,
                "updated": 0,
                "unchanged": 0,
                "total_documents": 0,
                "chunks": 0,
                "empty": True,
                "sample": [],
                "recovery_requested": True,
            }
        else:
            # Full validation before accepting a task; preview has zero network IO.
            try:
                preview = await asyncio.to_thread(service.preview, source_id, content)
            except SourceImportCapacityError as exc:
                fail(413, exc.public_message)
            except (ValueError, OSError):
                fail(400, "资料校验未通过，请检查版本、字段、原生数字帖 ID、时间及来源状态")
        self.results[source_id] = {"source_id": source_id, "state": "validating", **preview}

        async def run() -> None:
            try:
                self.results[source_id] = await service.import_data(source_id, content)
            except asyncio.CancelledError:
                self.results[source_id] = {"source_id": source_id, "state": "cancelled"}
            except SourceImportCapacityError as exc:
                self.results[source_id] = {
                    "source_id": source_id,
                    "state": "failed",
                    "error_code": exc.code,
                }
            except Exception:
                # No exception text: provider URLs/headers and adapter settings are not public data.
                self.results[source_id] = {"source_id": source_id, "state": "failed"}

        self.tasks[source_id] = asyncio.create_task(run(), name=f"source-import:{source_id}")
        return self.results[source_id]

    async def close(self, _app: web.Application) -> None:
        if self.admin.weibo_qr is not None:
            await self.admin.weibo_qr.aclose()
        if self.service is not None:
            for source_id, task in self.tasks.items():
                if not task.done() and not getattr(self.service, "active", lambda _id: True)(
                    source_id
                ):
                    task.cancel()
            await self.service.aclose()
        if self.tasks:
            await asyncio.gather(*self.tasks.values(), return_exceptions=True)


def imports(admin: Admin) -> SourceImports:
    if admin.source_imports is None:
        admin.source_imports = SourceImports(admin)
    return admin.source_imports


async def source_overview(admin: Admin) -> list[dict[str, Any]]:
    """Retained counts include disabled sources; never construct WikiSite for documents."""
    disk = admin.disk_config()
    service = admin.service("knowledge")
    info = await service.store.info() if service is not None else None
    if info is None and admin.runtime.db is not None:
        info = await PostgresKnowledgeStore(admin.runtime.db).info()
    namespaces = info.wikis if info is not None else {}
    counts: dict[str, Any] = {}
    if admin.runtime.db is not None:
        rows = await admin.runtime.db.pool.fetch(
            "SELECT wiki, count(DISTINCT doc_key) AS documents, max(date) AS latest_document_at "
            "FROM kb_chunks GROUP BY wiki"
        )
        counts = {str(row["wiki"]): dict(row) for row in rows}
    result = []
    primary = disk.wikis[0].id if disk.wikis else None
    for source in source_instances(disk):
        details = namespaces.get(source.id)
        count = counts.get(source.id, {})
        legacy = namespaces.get("") if source.id == primary else None
        legacy_count = counts.get("", {}) if source.id == primary else {}
        latest = max(
            (
                str(value)
                for value in (
                    count.get("latest_document_at"),
                    legacy_count.get("latest_document_at"),
                )
                if value is not None
            ),
            default=None,
        )
        result.append(
            {
                "id": source.id,
                "label": source.label,
                "platform": source.platform.id,
                "driver": source.driver,
                "mounted": source.mounted,
                "role": source.role,
                "capabilities": list(source.platform.capabilities),
                "knowledge": {
                    "documents": count.get("documents", 0) + legacy_count.get("documents", 0),
                    "chunks": (details.chunks if details else 0) + (legacy.chunks if legacy else 0),
                    "latest_document_at": latest,
                    "loaded_at": details.loaded_at if details else None,
                },
                "import_status": imports(admin).status(source.id) if not source.is_wiki else None,
                "collector_status": "not_configured" if not source.is_wiki else "wiki_managed",
            }
        )
    return result


def routes(admin: Admin) -> list[web.RouteDef]:
    manager = imports(admin)

    def target(source_id: str, *, documents: bool = False) -> SourceInstance:
        try:
            source = source_instance(admin.disk_config(), source_id)
        except ValueError:
            fail(404, "没有这个资料来源")
        if documents and source.is_wiki:
            fail(409, "Wiki 请在原 Wiki 管理页操作")
        return source

    def editable(body: dict[str, Any], *, creating: bool = False) -> dict[str, Any]:
        allowed = {"label", "role", "mounted"} | ({"id", "platform"} if creating else set())
        if body.keys() - allowed:
            fail(400, "不接受来源 ID/平台变更、地址、路径、代码或凭据等字段")
        if "label" in body and (
            not isinstance(body["label"], str)
            or not body["label"].strip()
            or len(body["label"]) > 40
        ):
            fail(400, "显示名称应为 1 到 40 个字")
        if "mounted" in body and not isinstance(body["mounted"], bool):
            fail(400, "mounted 应该是 true 或 false")
        if "role" in body and (not isinstance(body["role"], str) or body["role"] not in _ROLES):
            fail(400, "role 只能是 unknown、official 或 community")
        return {**body, **({"label": body["label"].strip()} if "label" in body else {})}

    async def save(items: list[dict[str, Any]]) -> None:
        if manager.active():
            fail(409, "导入任务运行中，完成或取消后才能修改来源配置")
        disk = admin.disk_config()
        path = disk.override_path(_SOURCE_FILE)
        previous = path.read_text("utf-8") if path is not None and path.is_file() else None
        try:
            write_source_instances(disk, items)
        except ValueError:
            fail(409, "来源配置无法保存，请检查覆写目录和来源设置")
        try:
            await admin.runtime.reload()
        except Exception:
            restore_override(disk, _SOURCE_FILE, previous)
            fail(409, "来源配置无法生效，已恢复原配置")

    async def list_sources(_request: web.Request) -> web.Response:
        disk = admin.disk_config()
        return reply(
            {
                "sources": await source_overview(admin),
                "writable": disk.overrides_dir is not None,
                "platforms": [
                    {
                        "id": item.id,
                        "name": item.name,
                        "description": item.description,
                        "driver": item.driver,
                        "capabilities": list(item.capabilities),
                    }
                    for item in platforms().values()
                    if item.driver == "documents"
                ],
            }
        )

    async def create(request: web.Request) -> web.Response:
        body = editable(await read_body(request, limit=4096), creating=True)
        source_id = body.get("id")
        if not isinstance(source_id, str) or not _ID.fullmatch(source_id):
            fail(400, "来源 ID 要小写字母开头，只用小写字母、数字、下划线，最多 32 字符")
        platform_id = body.get("platform")
        platform = platforms().get(platform_id) if isinstance(platform_id, str) else None
        if platform is None or platform.driver != "documents":
            fail(400, "只能登记已安装的文档来源平台")
        async with admin.source_mutation_lock:
            disk = admin.disk_config()
            existing = source_instances(disk)
            if source_id in {item.id for item in existing}:
                fail(409, "来源 ID 已被 Wiki 或其他资料来源占用")
            entries = [dict(item.raw) for item in existing if not item.is_wiki]
            entries.append(
                {"id": source_id, "label": source_id, "role": "unknown", "mounted": True, **body}
            )
            await save(entries)
        return reply(
            {
                "source": next(
                    item for item in await source_overview(admin) if item["id"] == source_id
                )
            },
            201,
        )

    async def update_source(source_id: str, body: dict[str, Any]) -> web.Response:
        async with admin.source_mutation_lock:
            target(source_id, documents=True)
            entries = [
                dict(item.raw) for item in source_instances(admin.disk_config()) if not item.is_wiki
            ]
            for item in entries:
                if item["id"] == source_id:
                    item.update(body)
            await save(entries)
        return reply(
            {
                "source": next(
                    item for item in await source_overview(admin) if item["id"] == source_id
                )
            }
        )

    async def update(request: web.Request) -> web.Response:
        body = editable(await read_body(request, limit=4096))
        return await update_source(request.match_info["id"], body)

    async def mount(request: web.Request) -> web.Response:
        body = await read_body(request, limit=4096)
        if set(body) != {"mounted"} or not isinstance(body.get("mounted"), bool):
            fail(400, "只接受 mounted: true 或 false")
        return await update_source(request.match_info["id"], body)

    async def content(request: web.Request) -> str:
        body = await read_body(request, limit=_MAX_BODY)
        if set(body) != {"content"} or not isinstance(body.get("content"), str):
            fail(400, "只接受 content 字符串，不接受 URL、服务器文件路径或凭据")
        try:
            size = len(body["content"].encode("utf-8"))
        except UnicodeEncodeError:
            fail(400, "上传资料必须是合法 UTF-8 文本")
        if size > _MAX_CONTENT:
            fail(413, "上传资料不能超过 10 MiB")
        return body["content"]

    async def preview(request: web.Request) -> web.Response:
        source_id = request.match_info["id"]
        target(source_id, documents=True)
        text = await content(request)
        try:
            result = await asyncio.to_thread(manager.get().preview, source_id, text)
        except SourceImportCapacityError as exc:
            fail(413, exc.public_message)
        except (ValueError, OSError):
            fail(400, "资料校验未通过，请检查版本、字段、原生数字帖 ID、时间及来源状态")
        return reply(result)

    async def start_import(request: web.Request) -> web.Response:
        source_id = request.match_info["id"]
        text = await content(request)
        async with admin.source_mutation_lock:
            source = target(source_id, documents=True)
            if not source.mounted:
                fail(409, "这个来源已停用，请先恢复")
            if not admin.has_database:
                fail(503, "确认导入需要已配置的知识库数据库")
            result = await manager.start(source_id, text)
        return reply(result, 202)

    async def status(request: web.Request) -> web.Response:
        source_id = request.match_info["id"]
        target(source_id, documents=True)
        return reply({"import_status": manager.status(source_id)})

    async def cancel(request: web.Request) -> web.Response:
        source_id = request.match_info["id"]
        target(source_id, documents=True)
        cancelled = manager.service.cancel(source_id) if manager.service is not None else False
        task = manager.tasks.get(source_id)
        if not cancelled and task is not None and not task.done():
            # Before the coroutine has entered import_data there is not yet a service handle.
            if (manager.status(source_id) or {}).get("state") == "validating":
                task.cancel()
                manager.results[source_id] = {"source_id": source_id, "state": "cancelled"}
                cancelled = True
        return reply({"cancelled": cancelled})

    return [
        *session_routes(admin),
        web.get("/api/sources", list_sources),
        web.post("/api/sources", create),
        web.put("/api/sources/{id}", update),
        web.post("/api/sources/{id}/mount", mount),
        web.post("/api/sources/{id}/preview", preview),
        web.post("/api/sources/{id}/import", start_import),
        web.get("/api/sources/{id}/status", status),
        web.post("/api/sources/{id}/cancel", cancel),
    ]
