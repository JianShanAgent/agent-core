"""登录、首页概况、系统信息、重新加载和重启、实时日志。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any

from aiohttp import web

from jianshanagent.admin.auth import COOKIE
from jianshanagent.admin.core import (
    Admin,
    fail,
    int_param,
    read_body,
    reply,
    text_param,
)
from jianshanagent.admin.queries import DURATION_EDGES, fill_series
from jianshanagent.core.logging import LOG_BUFFER, get_logger
from jianshanagent.core.trace import Trace, brief

logger = get_logger(__name__)

#: 首页能选的统计时段最长多少小时（90 天）
_MAX_HOURS = 24 * 90
#: 超过这么多小时就按天归档，不按小时
_DAILY_FROM_HOURS = 72
#: 登录请求最多这么大（里面只有一个令牌）
_LOGIN_BODY_BYTES = 4096


def trace_summary(trace: Trace, *, finished: bool) -> dict[str, Any]:
    """内存里的一条处理记录 → 列表里的一行（和数据库查出来的列表项字段一致）。"""
    row = trace.to_json(finished=finished)
    row.pop("steps")
    row["question"] = brief(row["question"])
    row["reply"] = brief(row["reply"])
    row["stage"] = trace.steps[-1].title or trace.steps[-1].kind if trace.steps else ""
    return row


def routes(admin: Admin) -> list[web.RouteDef]:
    async def session(request: web.Request) -> web.Response:
        client = request.remote or "?"
        return reply(
            {
                "authenticated": admin.auth.valid(request.cookies.get(COOKIE)),
                "locked_seconds": round(admin.auth.locked_for(client)),
            }
        )

    async def login(request: web.Request) -> web.Response:
        client = request.remote or "?"
        locked = admin.auth.locked_for(client)
        if locked > 0:
            fail(429, f"输错的次数太多了，{round(locked)} 秒后再试")
        body = await read_body(request, limit=_LOGIN_BODY_BYTES)  # 不用登录就能调：只读一点点
        if not admin.auth.check(str(body.get("token") or "").strip(), client):
            fail(401, "令牌不对")
        response = reply({"authenticated": True})
        response.set_cookie(
            COOKIE,
            admin.auth.issue(),
            max_age=int(admin.auth.session_seconds),
            httponly=True,
            samesite="Strict",
            path="/",
        )
        logger.info("admin_login", client=client)
        return response

    async def logout(request: web.Request) -> web.Response:
        # 不只是让这个浏览器把 Cookie 删掉：以前发出去的全部作废，被人抄走的那份也不能再用。
        # 手里的 Cookie 还有效才作数——这个接口不登录也能调，不能让随便谁都把管理员踢下线
        if admin.auth.valid(request.cookies.get(COOKIE)):
            saved = admin.auth.revoke()
            logger.info("admin_logout", client=request.remote or "?", saved=saved)
        response = reply({"authenticated": False})
        response.del_cookie(COOKIE, path="/")
        return response

    async def overview(request: web.Request) -> web.Response:
        hours = int_param(request, "hours", 24, 1, _MAX_HOURS)
        channel = text_param(request, "channel")
        daily = hours > _DAILY_FROM_HOURS
        tracer = admin.runtime.tracer
        active = [trace_summary(trace, finished=False) for trace in tracer.active.values()]
        result: dict[str, Any] = {
            "status": admin.status(),
            "range": {"hours": hours, "daily": daily, "channel": channel},
            "duration_edges": list(DURATION_EDGES),
            "active": active,
        }
        if not admin.has_database:
            # 没有数据库：只有内存里最近的几十条，没法做统计
            recent = [trace_summary(trace, finished=True) for trace in reversed(tracer.recent)]
            return reply({**result, "stats": None, "recent": recent[:10]})
        queries = admin.queries
        now = datetime.now(admin.timezone)
        since = now - timedelta(hours=hours)
        stats = await queries.overview(since, channel=channel, daily=daily)
        stats["series"] = fill_series(
            since.replace(tzinfo=None),
            now.replace(tzinfo=None),
            daily=daily,
            traces=stats.pop("trace_series"),
            calls=stats.pop("call_series"),
        )
        recent = await admin.labelled(await queries.traces(limit=10, channel=channel))
        result["active"] = await admin.labelled(active)
        return reply({**result, "stats": stats, "recent": recent})

    async def status(_request: web.Request) -> web.Response:
        """机器人的现状。页面的侧栏每隔一会儿来问一次，所以只给现成的；
        唯一要查库的是备份的情况，一分钟里最多查一次。"""
        await admin.refresh_backup()
        return reply(admin.status())

    async def system(_request: web.Request) -> web.Response:
        config = admin.config
        secrets: list[dict[str, Any]] = []

        def walk(node: dict[str, Any], prefix: str) -> None:
            for key, value in node.items():
                if isinstance(value, dict):
                    walk(value, f"{prefix}{key}.")
                else:
                    filled = bool(str(value or "").strip())
                    placeholder = str(value or "").upper().startswith("YOUR_")
                    secrets.append({"key": f"{prefix}{key}", "set": filled and not placeholder})

        walk(config.secrets, "")  # 只看填没填，值本身不出这个函数
        await admin.refresh_backup()
        tables = await admin.queries.tables() if admin.has_database else []
        return reply(
            {
                "status": admin.status(),
                "environment": admin.environment(),
                "secrets": secrets,
                "tables": tables,
            }
        )

    async def reload(_request: web.Request) -> web.Response:
        try:
            await admin.runtime.reload()
        except Exception as exc:
            logger.exception("admin_reload_failed")
            fail(
                409,
                "按现在的配置装不起来，机器人仍按原来的配置运行："
                + brief(f"{type(exc).__name__}: {exc}", 500),
            )
        logger.info("admin_reloaded")
        return reply({"status": admin.status()})

    async def restart(_request: web.Request) -> web.Response:
        logger.warning("admin_restart_requested")

        async def soon() -> None:
            await asyncio.sleep(0.5)  # 先让这次请求的回应发出去
            admin.runtime.request_stop(restart=True)

        task = asyncio.get_running_loop().create_task(soon())
        _restarting.add(task)
        return reply({"restarting": True})

    async def logs(request: web.Request) -> web.Response:
        after = int_param(request, "after", 0, 0, 2**62)
        limit = int_param(request, "limit", 1000, 1, 5000)
        # 机器人重启后序号从头数：页面手里的序号比这里最新的还大，就知道要从头取了
        if after > LOG_BUFFER.last:
            after = 0
        entries = LOG_BUFFER.recent(after=after, limit=limit)
        return reply(
            {
                "items": entries,
                "last": entries[-1]["seq"] if entries else LOG_BUFFER.last,
                "started_at": admin.runtime.started_at,
            }
        )

    _restarting: set[asyncio.Task[None]] = set()
    return [
        web.get("/api/session", session),
        web.post("/api/login", login),
        web.post("/api/logout", logout),
        web.get("/api/overview", overview),
        web.get("/api/status", status),
        web.get("/api/system", system),
        web.post("/api/system/reload", reload),
        web.post("/api/system/restart", restart),
        web.get("/api/logs", logs),
    ]
