"""Admin-only third-party application credentials and usage."""

from __future__ import annotations

import hashlib
import re
import secrets
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any

from aiohttp import web

from jianshanagent.admin.core import Admin, fail, read_body, reply

if TYPE_CHECKING:
    from jianshanagent.core.integrations import IntegrationStore

_APP_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}", re.ASCII)
_RANGES = {"today": 1, "7d": 7, "30d": 30}
_MAX_DATE_RANGE = 366


def _id(value: Any) -> str:
    if not isinstance(value, str) or not _APP_ID.fullmatch(value):
        fail(
            400,
            "应用 ID 必须以字母或数字开头，仅允许字母、数字、点、下划线和短横线（最多 64 字符）",
        )
    return value


def _name(value: Any) -> str:
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 80 or not value.isprintable():
        fail(400, "显示名称应为 1–80 个可打印字符")
    return value.strip()


def _date(value: str) -> date:
    if not _DATE.fullmatch(value):
        fail(400, "日期请使用 YYYY-MM-DD 格式")
    try:
        return date.fromisoformat(value)
    except ValueError:
        fail(400, "日期不合法")


def _reply(data: Any, status: int = 200) -> web.Response:
    response = reply(data, status)
    response.headers["Cache-Control"] = "no-store"
    return response


def _token() -> tuple[str, bytes]:
    token = secrets.token_urlsafe(32)
    return token, hashlib.sha256(token.encode("ascii")).digest()


def routes(admin: Admin) -> list[web.RouteDef]:
    def store() -> IntegrationStore:
        repo = admin.runtime.integrations
        if repo is None:
            fail(503, "接入管理和用量统计需要 PostgreSQL；本地 YAML 接入只能在配置文件中修改")
        return repo

    async def list_apps(_request: web.Request) -> web.Response:
        repo = admin.runtime.integrations
        if repo is not None:
            return _reply({"managed": True, "apps": await repo.list_apps()})
        return _reply(
            {
                "managed": False,
                "apps": [
                    {
                        "app_id": app_id,
                        "name": "平安志" if app_id == "pinganzhi" else app_id,
                        "enabled": True,
                        "created_at": None,
                        "last_request_at": None,
                    }
                    for app_id in admin.runtime.legacy_integration_ids
                ],
            }
        )

    async def create(request: web.Request) -> web.Response:
        repo = store()
        body = await read_body(request, limit=4096)
        if body.keys() != {"app_id", "name"}:
            fail(400, "仅接受 app_id 和 name")
        app_id, name = _id(body["app_id"]), _name(body["name"])
        token, digest = _token()
        app = await repo.create(app_id, name, digest)
        if app is None:
            fail(409, "应用 ID 已存在")
        return _reply({"app": app, "token": token}, 201)

    async def patch(request: web.Request) -> web.Response:
        repo = store()
        app_id = _id(request.match_info["app_id"])
        body = await read_body(request, limit=4096)
        if not body or body.keys() - {"name", "enabled"}:
            fail(400, "仅可修改显示名称或启用状态")
        name = _name(body["name"]) if "name" in body else None
        enabled = body.get("enabled")
        if "enabled" in body and not isinstance(enabled, bool):
            fail(400, "enabled 应为布尔值")
        app = await repo.update(app_id, name=name, enabled=enabled)
        if app is None:
            fail(404, "未找到该应用")
        if enabled is True and not app["enabled"]:
            fail(409, "应用没有有效令牌，请先重签令牌")
        return _reply({"app": app})

    async def rotate(request: web.Request) -> web.Response:
        repo = store()
        app_id = _id(request.match_info["app_id"])
        if await read_body(request, limit=4096):
            fail(400, "重签请求只接受空 JSON 对象")
        token, digest = _token()
        app = await repo.rotate(app_id, digest)
        if app is None:
            fail(404, "未找到该应用")
        return _reply({"app": app, "token": token})

    async def usage(request: web.Request) -> web.Response:
        repo = store()
        tz = admin.timezone
        today = datetime.now(tz).date()
        raw_from, raw_to = request.query.get("from"), request.query.get("to")
        preset = request.query.get("range", "today")
        if raw_from is not None or raw_to is not None:
            if raw_from is None or raw_to is None or "range" in request.query:
                fail(400, "日期范围需要同时指定 from 和 to，并且不能与 range 一起使用")
            since, until = _date(raw_from), _date(raw_to)
        else:
            if preset not in _RANGES:
                fail(400, "range 应为 today、7d 或 30d")
            until = today
            since = today - timedelta(days=_RANGES[preset] - 1)
        if not 0 <= (until - since).days < _MAX_DATE_RANGE or until == date.max:
            fail(400, "日期范围无效，最多查询 366 天")
        app_id = _id(request.query["app_id"]) if "app_id" in request.query else None
        start = datetime.combine(since, time.min, tz)
        end = datetime.combine(until + timedelta(days=1), time.min, tz)
        totals = await repo.usage(start, end, app_id)
        if app_id is not None and not totals["apps"]:
            fail(404, "未找到该应用")
        return _reply(
            {
                "from": since.isoformat(),
                "to": until.isoformat(),
                "timezone": admin.timezone_name,
                **totals,
            }
        )

    return [
        web.get("/api/integrations", list_apps),
        web.post("/api/integrations", create),
        web.get("/api/integrations/usage", usage),
        web.patch("/api/integrations/{app_id}", patch),
        web.post("/api/integrations/{app_id}/rotate", rotate),
    ]
