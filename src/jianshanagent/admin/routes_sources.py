"""通用资料来源：只登记受信任平台、接收标准资料，不拉取 URL 或执行上传代码。"""

from __future__ import annotations

import asyncio
import re
from contextlib import ExitStack
from typing import Any, cast
from urllib.parse import urlsplit

from aiohttp import web

from jianshanagent.admin.core import Admin, fail, read_body, reply
from jianshanagent.admin.routes_sources_collector import collection_status
from jianshanagent.admin.routes_sources_collector import routes as collector_routes
from jianshanagent.admin.routes_sources_session import routes as session_routes
from jianshanagent.core.config import AppConfig, restore_override, write_source_instances
from jianshanagent.core.manifest import platforms
from jianshanagent.core.sources import SourceInstance, source_instance, source_instances
from jianshanagent.plugins.knowledge.source_collect import (
    ACTIVE_STATES,
    SourceCollectError,
    acquire_source_operation,
    collector_active,
    collector_overview,
    source_operation_active,
)
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
    "content_type",
    "discussion_of",
    "post_url",
    "post_author_id",
    "evidence_group",
    "author_ids",
    "node_ids",
    "target_reply_ids",
    "discussion_nodes",
    "untrusted_content",
    "packet_id",
    "logical_packet_id",
    "fragment_id",
    "discussion_scope",
}


def safe_provenance(meta: Any) -> dict[str, Any]:
    """Only bounded reference metadata crosses the API, never arbitrary config."""
    if not isinstance(meta, dict):
        return {}
    node_fields = {
        "id",
        "root_id",
        "parent_id",
        "reply_to_id",
        "url",
        "text",
        "author_id",
        "author_name",
        "published_at",
        "is_target_reply",
        "text_start",
        "text_end",
        "text_length",
        "body_start",
        "body_end",
    }

    def clean(value: Any, depth: int = 0) -> Any:
        if isinstance(value, str):
            return value[:4000]
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, (list, tuple)) and depth < 3:
            return [clean(item, depth + 1) for item in value[:64]]
        if isinstance(value, dict) and depth < 3:
            return {
                str(key): safe_url(item) if key == "url" else clean(item, depth + 1)
                for key, item in value.items()
                if key in node_fields
            }
        return None

    result = {key: clean(value) for key, value in meta.items() if key in _META_FIELDS}
    for key in {"origin_url", "mirror_url", "post_url"} & result.keys():
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


def _with_discussion_sources(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Create a requested community partition atomically with its parent configuration."""
    result = [dict(item) for item in items]
    known = {item["id"]: item for item in result}
    for item in tuple(result):
        collector = item.get("collector") or {}
        if collector.get("mode") != "full_then_incremental":
            continue
        item["storage"] = "stream_v2"
        if not collector.get("include_comments"):
            continue
        child_id = collector["discussion_source_id"]
        if child_id == item["id"]:
            raise ValueError("讨论分区不能与正文来源相同")
        existing = known.get(child_id)
        if existing is not None:
            if (
                existing.get("discussion_of") != item["id"]
                or existing.get("platform") != "weibo"
                or existing.get("role") != "community"
                or existing.get("storage") != "stream_v2"
                or "collector" in existing
            ):
                raise ValueError("讨论分区 ID 已被其他来源占用，未覆盖现有配置")
            continue
        child = {
            "id": child_id,
            "label": str(item.get("label") or item["id"])[:35] + "·评论",
            "platform": "weibo",
            "mounted": item.get("mounted", True),
            "role": "community",
            "discussion_of": item["id"],
            "storage": "stream_v2",
        }
        result.append(child)
        known[child_id] = child
    return result


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
        if any(not task.done() for task in self.tasks.values()):
            return True
        disk = self.admin.disk_config()
        return any(
            collector_active(disk, source.id) or source_operation_active(disk, source.id)
            for source in source_instances(disk)
            if not source.is_wiki
        )

    def get(self) -> SourceImportService:
        database = self.admin.runtime.db
        if self.service is None or (database is not self._database and not self.active()):
            # These stores only wrap the runtime-owned connection pool; they own no connections.
            store = (
                PostgresKnowledgeStore(database) if database is not None else MemoryKnowledgeStore()
            )
            self.service = SourceImportService(
                self.config,
                store,
                config_provider=self.admin.disk_config,
            )
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
        disk = self.admin.disk_config()
        if collector_active(disk, source_id):
            fail(409, "该来源已有导入或采集任务，请先停止任务再导入")
        try:
            lease = acquire_source_operation(disk, source_id)
        except SourceCollectError as exc:
            fail(exc.status, exc.public_message)
        try:
            # A queued request releases its short lease; recheck the durable job while
            # holding our lease so a queue write racing the first check cannot slip in.
            if collector_overview(disk, source_id).get("state") in ACTIVE_STATES:
                fail(409, "该来源已有采集任务，请先停止任务再导入")
            service = self.get()
            # Recovery replays the last complete snapshot without new records.
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
                # The operation lease is already held before expensive validation starts.
                preview = await asyncio.to_thread(service.preview, source_id, content)
            self.results[source_id] = {"source_id": source_id, "state": "validating", **preview}
        except SourceImportCapacityError as exc:
            lease.close()
            fail(413, exc.public_message)
        except (ValueError, OSError):
            lease.close()
            fail(400, "资料校验未通过，请检查版本、字段、原生数字帖 ID、时间及来源状态")
        except BaseException:
            lease.close()
            raise

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
            finally:
                lease.close()

        self.tasks[source_id] = asyncio.create_task(run(), name=f"source-import:{source_id}")
        # A task cancelled before its coroutine starts never reaches the finally block.
        self.tasks[source_id].add_done_callback(lambda _task: lease.close())
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
        collection = collection_status(admin, source)
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
                "collector": source.raw.get("collector"),
                "discussion_of": source.raw.get("discussion_of"),
                "storage": source.raw.get("storage"),
                "collection": collection,
                "collector_status": (
                    collection["state"]
                    if collection is not None
                    else "wiki_managed"
                    if source.is_wiki
                    else "not_available"
                ),
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
        allowed = {"label", "role", "mounted", "collector", "storage", "discussion_of"} | (
            {"id", "platform"} if creating else set()
        )
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
        if "storage" in body and body["storage"] != "stream_v2":
            fail(400, "storage 仅允许 stream_v2")
        if "discussion_of" in body and (
            not isinstance(body["discussion_of"], str) or not _ID.fullmatch(body["discussion_of"])
        ):
            fail(400, "discussion_of 必须是合法的父来源 ID")
        normalized = {**body, **({"label": body["label"].strip()} if "label" in body else {})}
        if "collector" in body:
            from jianshanagent.weibo_collector_config import parse_weibo_collector

            try:
                normalized["collector"] = parse_weibo_collector(body["collector"])
            except ValueError:
                fail(400, "微博采集配置无效，请检查目标 UID、字段类型与允许范围")
        return normalized

    async def save(items: list[dict[str, Any]]) -> None:
        if manager.active():
            fail(409, "来源导入或采集运行中，请先停止任务再修改配置")
        disk = admin.disk_config()
        path = disk.override_path(_SOURCE_FILE)
        previous = path.read_text("utf-8") if path is not None and path.is_file() else None
        # Hold every existing documents namespace across write + reload. The check alone races
        # with a worker starting in another process; acquiring its exact lease closes that gap.
        with ExitStack() as leases:
            try:
                held_groups: set[str] = set()
                for source in source_instances(disk):
                    group = source.raw.get("discussion_of") or source.id
                    if not source.is_wiki and group not in held_groups:
                        leases.enter_context(acquire_source_operation(disk, source.id))
                        held_groups.add(group)
            except SourceCollectError as exc:
                fail(exc.status, exc.public_message)
            try:
                write_source_instances(disk, _with_discussion_sources(items))
            except ValueError:
                fail(409, "来源配置无法保存；已绑定 UID 不能更换或移除，历史作者不明请新建来源")
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
        if "collector" in body and platform.id != "weibo":
            fail(400, "只有微博平台可以配置微博采集目标")
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
            source = target(source_id, documents=True)
            if "collector" in body and source.platform.id != "weibo":
                fail(400, "只有微博平台可以配置微博采集目标")
            if "collector" in body and admin.runtime.db is not None:
                uid = body["collector"]["uid"]
                try:
                    owners = await admin.runtime.db.pool.fetch(
                        "SELECT DISTINCT meta->>'author_id' AS author_id "
                        "FROM kb_chunks WHERE wiki = $1",
                        source_id,
                    )
                except Exception:
                    fail(503, "暂时无法核验已有资料作者，未修改来源配置")
                if any(row["author_id"] != uid for row in owners):
                    fail(409, "已有资料作者与目标 UID 不一致或无法确认，请新建来源")
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
        return cast(str, body["content"])

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
        *collector_routes(admin),
        web.get("/api/sources", list_sources),
        web.post("/api/sources", create),
        web.put("/api/sources/{id}", update),
        web.post("/api/sources/{id}/mount", mount),
        web.post("/api/sources/{id}/preview", preview),
        web.post("/api/sources/{id}/import", start_import),
        web.get("/api/sources/{id}/status", status),
        web.post("/api/sources/{id}/cancel", cancel),
    ]
