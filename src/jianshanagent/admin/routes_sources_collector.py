"""采集控制仅投递共享队列；抓取与入库由 kbsync 唯一执行。"""

from __future__ import annotations

from typing import Any

from aiohttp import web

from jianshanagent.admin.core import Admin, fail, read_body, reply
from jianshanagent.core.sources import SourceInstance, source_instance
from jianshanagent.plugins.knowledge import source_collect

_STATUS_FIELDS = frozenset(
    {
        "state",
        "phase",
        "job_id",
        "uid",
        "profile_url",
        "pages",
        "fetched",
        "accepted",
        "new",
        "updated",
        "unchanged",
        "started_at",
        "finished_at",
        "last_success_at",
        "next_run_at",
        "error_code",
        "message",
        "limited",
        "heartbeat_at",
        "source_id",
    }
)


def public_collection(status: dict[str, Any]) -> dict[str, Any]:
    """第二道公开字段白名单，不返回请求签名、断点、凭据或任意额外字段。"""
    return {key: value for key, value in status.items() if key in _STATUS_FIELDS}


def collection_status(admin: Admin, source: SourceInstance) -> dict[str, Any] | None:
    if source.platform.id != "weibo" or source.is_wiki:
        return None
    try:
        status = public_collection(
            source_collect.collector_overview(admin.disk_config(), source.id)
        )
        if "collector" not in source.raw:
            status.update(state="not_configured", phase="not_configured")
        return status
    except source_collect.SourceCollectError as exc:
        return {
            "source_id": source.id,
            "state": "failed",
            "phase": "failed",
            "error_code": exc.code,
            "message": exc.public_message,
        }


def routes(admin: Admin) -> list[web.RouteDef]:
    def target(source_id: str) -> SourceInstance:
        try:
            source = source_instance(admin.disk_config(), source_id)
        except ValueError:
            fail(404, "没有这个资料来源")
        if source.platform.id != "weibo" or source.is_wiki:
            fail(409, "这个平台不支持微博采集")
        return source

    async def status(request: web.Request) -> web.Response:
        return reply(collection_status(admin, target(request.match_info["id"])))

    async def start(request: web.Request) -> web.Response:
        body = await read_body(request, limit=4096)
        if body:
            fail(400, "采集请求只接受空 JSON 对象，目标与参数请在来源配置中保存")
        source_id = request.match_info["id"]
        async with admin.source_mutation_lock:
            source = target(source_id)
            if "collector" not in source.raw:
                fail(409, "请先保存这个来源的微博目标 UID")
            if not admin.has_database:
                fail(503, "采集入库需要已配置的知识库数据库")
            try:
                result = source_collect.request_collect(admin.disk_config(), source_id)
            except source_collect.SourceCollectError as exc:
                fail(exc.status, exc.public_message)
        return reply(public_collection(result), 202)

    async def cancel(request: web.Request) -> web.Response:
        body = await read_body(request, limit=4096)
        job_id = body.get("job_id")
        if set(body) != {"job_id"} or not isinstance(job_id, str) or not 1 <= len(job_id) <= 128:
            fail(400, "取消请求只接受当前任务的 job_id")
        source_id = request.match_info["id"]
        target(source_id)
        # Never take the reload/mutation lock: stopping remains available during an operation.
        try:
            result = source_collect.cancel_collect(admin.disk_config(), source_id, job_id)
        except source_collect.SourceCollectError as exc:
            fail(exc.status, exc.public_message)
        return reply(public_collection(result))

    return [
        web.post("/api/sources/{id}/collect", start),
        web.get("/api/sources/{id}/collector", status),
        web.post("/api/sources/{id}/collect/cancel", cancel),
    ]
