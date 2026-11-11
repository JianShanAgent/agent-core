"""管理后台各个接口共用的东西：上下文、返回 JSON、报错、取参数、登录把关。"""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn
from zoneinfo import ZoneInfo

from aiohttp import web

from jianshanagent import __version__
from jianshanagent.admin.auth import COOKIE, CSRF_HEADER, CSRF_VALUE, Auth
from jianshanagent.admin.queries import AdminQueries
from jianshanagent.core.config import AppConfig, load_config
from jianshanagent.core.logging import get_logger
from jianshanagent.core.plugin import PluginHost
from jianshanagent.core.trace import brief

if TYPE_CHECKING:
    from jianshanagent.runtime import Runtime

logger = get_logger(__name__)

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]
STATIC_DIR = Path(__file__).resolve().parent / "static"
#: 数据库备份的情况最多隔多久去库里看一次（秒）
_BACKUP_CHECK_SECONDS = 60.0
#: 不登录也能访问的接口
_PUBLIC = {"/api/session", "/api/login", "/api/logout"}


class ApiError(Exception):
    """接口要回给页面的错误：状态码和一句给人看的话。"""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def fail(status: int, message: str) -> NoReturn:
    raise ApiError(status, message)


def _jsonable(value: Any) -> Any:
    """JSON 不认识的值：时间写成 ISO 格式（浏览器认），别的转成文字。"""
    return value.isoformat() if isinstance(value, datetime) else str(value)


def reply(data: Any, status: int = 200) -> web.Response:
    """返回 JSON。"""
    return web.Response(
        text=json.dumps(data, ensure_ascii=False, default=_jsonable),
        status=status,
        content_type="application/json",
    )


async def read_body(request: web.Request, *, limit: int = 0) -> dict[str, Any]:
    """读 JSON 请求体。``limit``：最多读这么多字节，超了回 413（0 = 按整个服务的上限）。

    整个服务的上限为了网页对话传图放得很大；不用登录就能调的接口要自己给一个小的上限，
    不然谁都能让进程把几十 MB 读进内存、解析时卡住所有消息。
    """
    try:
        if limit:
            if (request.content_length or 0) > limit:
                fail(413, "请求内容太大了")
            raw = b""
            while len(raw) <= limit:
                piece = await request.content.read(limit + 1 - len(raw))
                if not piece:
                    break
                raw += piece
            if len(raw) > limit:
                fail(413, "请求内容太大了")
            data = json.loads(raw)
        else:
            data = await request.json()
    except (ValueError, UnicodeDecodeError, RecursionError):  # 嵌套太深的 JSON 是 RecursionError
        fail(400, "请求内容不是合法的 JSON")
    if not isinstance(data, dict):
        fail(400, "请求内容应该是一个 JSON 对象")
    return data


def int_param(request: web.Request, name: str, default: int, low: int, high: int) -> int:
    raw = request.query.get(name)
    if raw is None or raw == "":
        return default
    try:
        return max(low, min(high, int(raw)))
    except ValueError:
        fail(400, f"参数 {name} 应该是整数")


def text_param(request: web.Request, name: str) -> str | None:
    """没给或者是空的都算没给。"""
    value = request.query.get(name, "").strip()
    return value or None


def flag_param(request: web.Request, name: str) -> bool:
    return request.query.get(name, "").lower() in {"1", "true", "yes"}


def time_param(request: web.Request, name: str) -> datetime | None:
    raw = text_param(request, name)
    if raw is None:
        return None
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        fail(400, f"参数 {name} 不是合法的时间")
    return moment if moment.tzinfo else moment.astimezone()


@dataclass(frozen=True, slots=True)
class AdminSettings:
    token_file: str = "data/admin/token"
    session_days: float = 30.0
    max_failures: int = 5
    lock_seconds: float = 60.0
    web_chat_user: str = "admin"
    logs_url: str = ""
    #: 超过这么多小时没有一次成功的数据库备份就提醒；0 = 不提醒
    backup_warn_hours: float = 26.0

    @classmethod
    def from_config(cls, config: AppConfig) -> AdminSettings:
        raw = config.get("core.admin")
        defaults = cls()
        login = raw.get("login") or {}
        token_file = (
            os.environ.get("JIANSHANAGENT_ADMIN_TOKEN_FILE")
            or raw.get("token_file")
            or defaults.token_file
        )
        return cls(
            token_file=str(token_file),
            session_days=float(raw.get("session_days") or defaults.session_days),
            max_failures=int(login.get("max_failures") or defaults.max_failures),
            lock_seconds=float(login.get("lock_seconds") or defaults.lock_seconds),
            web_chat_user=str((raw.get("web_chat") or {}).get("user") or defaults.web_chat_user),
            logs_url=str(raw.get("logs_url") or ""),
            backup_warn_hours=max(
                0.0, float(raw.get("backup_warn_hours", defaults.backup_warn_hours) or 0)
            ),
        )


class Admin:
    """各个接口拿到的上下文。

    机器人的那一套（配置、插件、路由）会重新装配，所以每次用的时候都从 ``runtime`` 上现取，
    不在这里存引用。
    """

    def __init__(self, runtime: Runtime, settings: AdminSettings, auth: Auth, token_source: str):
        self.runtime = runtime
        self.settings = settings
        self.auth = auth
        #: 登录令牌是哪来的：secrets / file / generated
        self.token_source = token_source
        #: 上一次查到的备份情况（见 :meth:`refresh_backup`）和查的时间
        self._backup: dict[str, Any] | None = None
        self._backup_checked: float | None = None

    @property
    def config(self) -> AppConfig:
        return self.runtime.config

    @property
    def host(self) -> PluginHost:
        return self.runtime.host

    @property
    def timezone_name(self) -> str:
        return str(self.config.app.get("timezone") or "Asia/Shanghai")

    @property
    def timezone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone_name)

    @property
    def has_database(self) -> bool:
        return self.runtime.db is not None

    @property
    def queries(self) -> AdminQueries:
        """查数据库。没有数据库（本机没配）时报错，页面上显示「需要数据库」。"""
        if self.runtime.db is None:
            fail(503, "这一页要用数据库，现在没有配置数据库")
        return AdminQueries(self.runtime.db, self.timezone_name)

    def disk_config(self) -> AppConfig:
        """盘上现在的配置（刚存完、还没重新装配或者要重启才生效的改动，这里看得到）。"""
        return load_config(self.config.root, self.config.overrides_dir)

    def service(self, name: str) -> Any:
        return self.host.services.get(name)

    async def labelled(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """给带 chat_id / user_id 的行补上备注名（``chat_label`` / ``user_label``）和昵称。

        平台给的标识是一长串看不懂的字符，页面上优先显示备注名。没有数据库时原样返回。
        """
        if self.runtime.db is None or not items:
            return items
        queries = self.queries
        labels = await queries.labels()
        names = await queries.user_names(
            sorted({str(item["user_id"]) for item in items if item.get("user_id")})
        )
        for item in items:
            chat_id, user_id = str(item.get("chat_id") or ""), str(item.get("user_id") or "")
            item["chat_label"] = labels["chat"].get(chat_id, "")
            # 单聊里对方就是这个会话：给会话起的名字也就是他的名字
            item["user_label"] = labels["user"].get(user_id, "") or (
                item["chat_label"] if item.get("scope") == "c2c" else ""
            )
            if not item.get("user_name") and user_id in names:
                item["user_name"] = names[user_id]
        return items

    async def refresh_backup(self) -> None:
        """看一眼数据库备份的情况，记下来给 :meth:`status` 用。

        页面每隔几秒就来问一次现状，所以一分钟里最多查一次库。查不成（数据库正好连不上）
        不影响页面别的部分：沿用上一次查到的，记一条日志。
        """
        if self.runtime.db is None:
            self._backup = None
            return
        now = time.monotonic()
        if self._backup_checked is not None and now - self._backup_checked < _BACKUP_CHECK_SECONDS:
            return
        self._backup_checked = now
        try:
            self._backup = await self.queries.backup_state()
        except Exception as exc:
            logger.warning("admin_backup_state_failed", error=brief(repr(exc)))

    def backup(self) -> dict[str, Any] | None:
        """数据库备份的情况，给页面看的。没有数据库、或者还没查过时是 None。

        ``overdue``：超过 ``backup_warn_hours`` 小时没有一次成功的备份。一次成功的都没有时，
        从开始有备份记录那一刻算起——刚部署、第一次备份还没到点，不算。
        """
        found = self._backup
        if found is None:
            return None
        hours = self.settings.backup_warn_hours
        reference = found["last_ok_at"] or found["since"]
        waited = (datetime.now(reference.tzinfo) - reference).total_seconds() if reference else 0.0
        return {**found, "warn_hours": hours, "overdue": hours > 0 and waited > hours * 3600}

    def status(self) -> dict[str, Any]:
        """机器人的现状，首页和系统页都用。"""
        runtime = self.runtime
        gateway = runtime.gateway
        disk = self.disk_config()
        services = self.host.services
        now = time.time()
        return {
            "version": __version__,
            "release": os.environ.get("JIANSHANAGENT_RELEASE") or None,
            "now": now,
            "started_at": runtime.started_at,
            "uptime_seconds": now - runtime.started_at,
            "loaded_at": runtime.loaded_at,
            "generation": runtime.generation,
            "dry_run": runtime.dry_run,
            "timezone": self.timezone_name,
            "gateway": None
            if gateway is None
            else {
                "connected": gateway.connected,
                "connected_at": gateway.connected_at,
                "disconnects": gateway.disconnects,
                "events": gateway.events,
                "last_event_at": gateway.last_event_at,
            },
            "database": runtime.db is not None,
            "llm": runtime.llm is not None,
            "plugins": [plugin.id for plugin in self.host.loaded],
            "features": {
                "research": "research" in services,
                "knowledge": "knowledge" in services,
                "image_search": "image_search" in services,
                "memory": "memory" in services,
                "chatlog": "chatlog" in services,
                "wiki": "wiki_query_client" in services,
                "tracing": runtime.tracer.sink is not None,
            },
            "active": len(runtime.tracer.active),
            "backup": self.backup(),
            "config": {
                "writable": disk.overrides_dir is not None,
                "overrides": sorted(disk.overrides),
                "override_errors": disk.override_errors,
                "overrides_ignored": runtime.overrides_ignored,
                "pending_restart": runtime.pending_restart(disk),
                "pending_reload": runtime.pending_reload(disk),
            },
        }

    def environment(self) -> dict[str, Any]:
        config = self.config
        return {
            "python": sys.version.split()[0],
            "platform": sys.platform,
            "pid": os.getpid(),
            "in_container": Path("/.dockerenv").exists(),
            "config_dir": str(config.root),
            "overrides_dir": str(config.overrides_dir) if config.overrides_dir else None,
            "static_ready": (STATIC_DIR / "index.html").is_file(),
            "token_source": self.token_source,
            "logs_url": self.settings.logs_url,
        }


def guard(admin: Admin) -> Any:
    """接口的把关：没登录的挡在外面，改东西的请求要带自定义头；出错统一回一句人话。"""

    @web.middleware
    async def middleware(request: web.Request, handler: Handler) -> web.StreamResponse:
        if not request.path.startswith("/api/"):
            return await handler(request)
        try:
            if request.path not in _PUBLIC:
                if not admin.auth.valid(request.cookies.get(COOKIE)):
                    fail(401, "还没登录，或者登录过期了")
                if request.method not in {"GET", "HEAD"} and (
                    request.headers.get(CSRF_HEADER) != CSRF_VALUE
                ):
                    fail(403, "请求缺少必要的请求头")
            return await handler(request)
        except ApiError as exc:
            return reply({"error": exc.message}, exc.status)
        except web.HTTPException as exc:
            if exc.status >= 400:
                return reply({"error": exc.reason or "请求出错"}, exc.status)
            raise
        except Exception as exc:
            logger.exception("admin_api_failed", path=request.path, method=request.method)
            return reply({"error": f"后台出错了：{brief(f'{type(exc).__name__}: {exc}')}"}, 500)

    return middleware
