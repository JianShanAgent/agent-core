"""进程的装配和运行。

读配置 → 连数据库 → 装模型、插件、路由 → 起 HTTP（健康检查，管理后台也挂在上面）→ 连 QQ。

模型、插件、路由这一套可以不重启进程重新装（:meth:`Runtime.reload`）：管理后台里改了配置，
按新配置装出新的一套，装成了再整套换上去；装不成（配置写错了）就什么都不换，原来的照常运行。
换下来的那一套过几分钟才关连接，让还在用它的消息处理完。数据库连接、QQ 连接、HTTP 服务不重装，
所以 ``app.yaml``、``core.storage.yaml``、``gateway.qq.yaml``、``core.admin.yaml`` 的改动要重启进程
才生效（:meth:`Runtime.request_stop` 带 ``restart=True``，见 main.py）。

覆写目录里的配置（管理后台改的，见 core/config.py）如果让机器人起不来，启动时退回只用默认值，
原因记在 :attr:`Runtime.overrides_ignored` 里，管理后台会显示出来：网页上改错配置不能把机器人改死。
「起不来」包括那几份要重启才生效的配置里写错了值（端口、回复次数之类）——它们保存时没有重新装配
这一关，所以启动时在同一处检查（:class:`_Boot`）。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from dataclasses import dataclass
from typing import Any

from aiohttp import web

from jianshanagent.core.config import AppConfig, load_config
from jianshanagent.core.conversation import MemoryConversationStore, PostgresConversationStore
from jianshanagent.core.db import Database
from jianshanagent.core.llm import LLMClient, LLMSettings
from jianshanagent.core.logging import get_logger, setup_logging
from jianshanagent.core.messages import InboundMessage, OutboundMessage
from jianshanagent.core.plugin import EraserSpec, PluginHost
from jianshanagent.core.router import MessageRouter
from jianshanagent.core.trace import Tracer, brief
from jianshanagent.core.trace_store import PostgresTraceStore
from jianshanagent.core.update import Updater, UpdateSettings
from jianshanagent.gateway.qq import QQGateway

logger = get_logger(__name__)

#: 改了要重启进程才生效的配置文件（其余的重新装配就生效）
RESTART_ONLY = frozenset({"app", "core.storage", "gateway.qq", "core.admin"})
#: 重新装配后，换下来的那一套过这么久才关连接：一次查证最长 4 分钟，等用着它的消息处理完
RETIRE_SECONDS = 300.0
_PRUNE_EVERY_SECONDS = 6 * 3600.0
#: 管理后台的网页对话可以上传图片（最多 4 张、每张 10 MB，转成文字传上来要多占三分之一），
#: 请求体放宽到这么大（aiohttp 默认 1 MB）
_MAX_REQUEST_BYTES = 64 * 1024 * 1024
#: 进程退出时，HTTP 服务最多等还没断开的连接这么久（aiohttp 默认等 60 秒）
_WEB_SHUTDOWN_SECONDS = 3.0


@dataclass(frozen=True, slots=True)
class _Boot:
    """对外那一步（监听端口、连 QQ）要用的设置。这些来自「要重启才生效」的那几份配置。"""

    health_host: str
    health_port: int
    health_path: str
    max_replies: dict[str, int]
    drain_seconds: float
    session_store_dir: str | None
    #: 连不连 QQ（gateway.qq.yaml 的 enabled）。关掉以后进程照常起来，管理后台和网页对话能用
    gateway_enabled: bool = True
    #: 数据库连接池的大小（core.storage.yaml）
    pool_min: int = 1
    pool_max: int = 10

    @classmethod
    def from_config(cls, config: AppConfig) -> _Boot:
        """读出来并检查。写错了在这里就报，不等到监听端口、连 QQ 时才出事。"""
        health = config.app.get("healthcheck") or {}
        port = int(health.get("port") or 8080)
        path = str(health.get("path") or "/healthz")
        if not 0 < port < 65536:
            raise ValueError(f"app.yaml 的 healthcheck.port 不是合法的端口：{port}")
        if not path.startswith("/"):
            raise ValueError(f"app.yaml 的 healthcheck.path 要以 / 开头：{path}")
        gateway = config.get("gateway.qq")
        passive = (gateway.get("reply") or {}).get("passive") or {}
        drain = (gateway.get("shutdown") or {}).get("drain_seconds")
        store_dir = os.environ.get("JIANSHANAGENT_SESSION_STORE_DIR") or gateway.get(
            "session_store_dir"
        )
        postgres = config.get("core.storage").get("postgres") or {}
        pool_min, pool_max = int(postgres.get("pool_min") or 1), int(postgres.get("pool_max") or 10)
        if not 1 <= pool_min <= pool_max:
            raise ValueError(
                f"core.storage.yaml 的连接池大小不对：pool_min={pool_min}，pool_max={pool_max}"
                "（要 1 ≤ pool_min ≤ pool_max）"
            )
        if config.get("core.admin").get("enabled", False):
            from jianshanagent.admin.core import AdminSettings

            AdminSettings.from_config(config)  # 只为了检查：里面的数字写错了在这里报
        return cls(
            health_host=str(health.get("host") or "0.0.0.0"),
            health_port=port,
            health_path=path,
            max_replies={
                "c2c": int(passive.get("c2c_max_replies") or 1),
                "group": int(passive.get("group_max_replies") or 1),
            },
            drain_seconds=max(float(drain), 0.0) if drain is not None else 20.0,
            session_store_dir=str(store_dir) if store_dir else None,
            gateway_enabled=bool(gateway.get("enabled", True)),
            pool_min=pool_min,
            pool_max=pool_max,
        )


@dataclass(slots=True)
class _Assembly:
    """按一份配置装出来的一套。"""

    config: AppConfig
    host: PluginHost
    router: MessageRouter
    llm: LLMClient | None
    #: 处理记录记不记、提示词留不留（core.trace.yaml）。装配时就读好：
    #: 换上去的那一步不能再出错，不然换了一半、接口却报「已撤回」
    trace_enabled: bool = True
    keep_prompts: bool = True


async def _close_services(host: PluginHost, keep: set[int]) -> None:
    """关掉一套里带网络连接的服务（wiki、知识库检索、模型…）；``keep`` 里的是各套共用的，不关。"""
    for service in host.services.values():
        if id(service) in keep or not hasattr(service, "aclose"):
            continue
        try:
            await service.aclose()
        except Exception:
            logger.exception("service_close_failed", service=type(service).__name__)


class Runtime:
    def __init__(self, *, dry_run: bool | None = None):
        if dry_run is None:
            dry_run = os.environ.get("JIANSHANAGENT_DRY_RUN", "").lower() in {"1", "true", "yes"}
        #: 不连 QQ（本机调试用：同一个机器人不能两处同时在线）
        self.dry_run = dry_run
        self.started_at = time.time()
        #: 现在这一套是什么时候装的、第几套
        self.loaded_at = self.started_at
        self.generation = 0
        self.config: AppConfig
        self.host: PluginHost
        self.router: MessageRouter
        self.llm: LLMClient | None = None
        self.db: Database | None = None
        self.tracer = Tracer()
        self.trace_store: PostgresTraceStore | None = None
        self.gateway: QQGateway | None = None
        #: 覆写目录里的配置让机器人起不来、被整个忽略时的原因；没有这回事就是空的
        self.overrides_ignored = ""
        #: 进程启动时，那几份「要重启才生效」的配置是什么样（用来看盘上的改动生效了没有）
        self._boot_files: dict[str, dict[str, Any]] = {}
        self._boot: _Boot
        self.restart_requested = False
        self._conversations: Any = None
        self._stop = asyncio.Event()
        self._reload_lock = asyncio.Lock()
        self._retiring: set[asyncio.Task[None]] = set()
        self._retired: list[PluginHost] = []
        self._web: web.AppRunner | None = None
        self._maintenance: asyncio.Task[None] | None = None
        #: 查主仓库有没有新版本、装更新包（见 core/update.py）。每次用的时候现读配置
        self.updater = Updater(lambda: UpdateSettings.from_config(self.config))
        self._update_check: asyncio.Task[None] | None = None

    # ---- 装配 ----

    def _shared(self) -> set[int]:
        """各套共用、不随重新装配关掉的服务。"""
        return {id(self.tracer), id(self._conversations), id(self.db), id(self)}

    async def _assemble(self, config: AppConfig) -> _Assembly:
        """按 ``config`` 装出一套。装不成就把半成品关掉、把错误抛出去，现在在用的不受影响。"""
        host = PluginHost(config)
        host.services["tracer"] = self.tracer
        host.services["runtime"] = self
        host.services["conversation_store"] = self._conversations
        if self.db is not None:
            host.services["database"] = self.db
        if self.trace_store is not None:
            # 处理记录里有用户说过的话：用户要求删除他的数据时（/清除记忆）一并删掉
            host.erasers.append(EraserSpec("处理记录", self._erase_traces, "core"))
        try:
            # LLM 在插件加载前注册，插件 register() 时即可取用；未启用时不注册
            llm_settings = LLMSettings.from_config(config)
            llm = LLMClient(llm_settings) if llm_settings else None
            if llm is not None:
                llm.on_call = self.tracer.record_call
                host.services["llm"] = llm
            host.load()
            if llm is not None and host.chat_handler is not None:
                # 对话入口固定用 chat 这一档；没有它的话，要等到有人说话时才报错
                if "chat" not in llm.settings.profiles:
                    raise ValueError("core.llm.yaml 的 profiles 里没有 chat 这一档（对话入口用它）")
            router = MessageRouter(host, config.get("core.router"))
            trace_cfg = config.get("core.trace")
            prompts = trace_cfg.get("prompts") or {}
            if not isinstance(prompts, dict):
                raise ValueError("core.trace.yaml 的 prompts 应该是键值结构")
            trace_enabled = bool(trace_cfg.get("enabled", True))
            keep_prompts = bool(prompts.get("keep", True))
            for name in ("retention_days",):  # 定期清理要用的数字，写错了也在这里报
                int(trace_cfg.get(name) or 0)
                int(prompts.get(name) or 0)
        except Exception:
            await _close_services(host, self._shared())
            raise
        return _Assembly(
            config=config,
            host=host,
            router=router,
            llm=llm,
            trace_enabled=trace_enabled,
            keep_prompts=keep_prompts,
        )

    async def _erase_traces(self, message: InboundMessage) -> int:
        """删掉这个用户的处理记录（他在所有群里的），返回删了几条。"""
        assert self.trace_store is not None
        return await self.trace_store.erase_user(message.session_key, message.user_id)

    def _install(self, assembly: _Assembly) -> None:
        """整套换上去。中间没有等待，对正在进来的消息来说是一下子换的。"""
        self.config = assembly.config
        self.host = assembly.host
        self.router = assembly.router
        self.llm = assembly.llm
        self.tracer.sink = self.trace_store if assembly.trace_enabled else None
        self.tracer.keep_prompts = assembly.keep_prompts
        self.generation += 1
        self.loaded_at = time.time()

    async def reload(self) -> None:
        """重新读配置，装一套新的换上去。装不成抛异常，原来的一套照常运行。"""
        async with self._reload_lock:
            config = load_config()
            assembly = await self._assemble(config)
            old = self.host
            self._install(assembly)
            self.overrides_ignored = ""
            self._retire(old)
            logger.info(
                "runtime_reloaded",
                generation=self.generation,
                plugins=[plugin.id for plugin in self.host.loaded],
                overrides=sorted(config.overrides),
            )

    def pending_restart(self, disk: AppConfig) -> list[str]:
        """``disk``（盘上现在的配置）里，哪几份和进程启动时的不一样、要重启才生效。"""
        return sorted(
            name
            for name in RESTART_ONLY
            if disk.files.get(name, {}) != self._boot_files.get(name, {})
        )

    def pending_reload(self, disk: AppConfig) -> list[str]:
        """``disk`` 里，哪几份和正在用的不一样、重新装配就生效。"""
        return sorted(
            name
            for name, values in disk.files.items()
            if name not in RESTART_ONLY and values != self.config.files.get(name)
        )

    def _retire(self, host: PluginHost) -> None:
        """换下来的一套：等还在用它的消息处理完，再关它的连接。"""
        self._retired.append(host)

        async def later() -> None:
            await asyncio.sleep(RETIRE_SECONDS)
            self._retired.remove(host)
            await _close_services(host, self._shared())

        task = asyncio.get_running_loop().create_task(later())
        self._retiring.add(task)
        task.add_done_callback(self._retiring.discard)

    # ---- 启动、停止 ----

    async def start(self) -> None:
        await self.prepare()
        await self.serve()

    async def prepare(self) -> None:
        """读配置、连数据库、装好第一套。不对外：不监听端口，不连 QQ。"""
        config = load_config()
        app_cfg = config.app
        log_cfg = app_cfg.get("log") or {}
        env_json = os.environ.get("JIANSHANAGENT_LOG_JSON", "").strip().lower()
        json_logs = (
            env_json in {"1", "true", "yes"} if env_json else bool(log_cfg.get("json", False))
        )
        setup_logging(
            level=str(log_cfg.get("level") or "INFO"),
            json_logs=json_logs,
        )
        logger.info(
            "config_loaded",
            config_dir=str(config.root),
            env=app_cfg.get("env"),
            overrides_dir=str(config.overrides_dir) if config.overrides_dir else None,
            overrides=sorted(config.overrides),
        )
        for name, reason in config.override_errors.items():
            logger.error("config_override_skipped", name=name, reason=reason)

        # 「要重启才生效」的那几份配置先读出来检查一遍：连数据库要用里面的连接池大小。
        # 管理后台改的值让它过不了检查时，这几项先按默认值来；整套退回默认值在下面装配时做
        try:
            boot = _Boot.from_config(config)
        except Exception:
            if not config.overrides:
                raise
            boot = _Boot.from_config(load_config(with_overrides=False))

        # 配了数据库就把对话历史、处理记录落库；没配（本机调试）时对话历史放内存
        if config.postgres_dsn:
            self.db = Database(config.postgres_dsn, pool_min=boot.pool_min, pool_max=boot.pool_max)
            await self.db.connect()
            await self.db.migrate()
            self._conversations = PostgresConversationStore(self.db)
            self.trace_store = PostgresTraceStore(self.db)
        else:
            self._conversations = MemoryConversationStore()

        try:
            boot = _Boot.from_config(config)
            assembly = await self._assemble(config)
        except Exception as exc:
            if not config.overrides:
                raise
            # 管理后台改的配置让机器人起不来：不能就这么停在那里，退回只用默认值
            self.overrides_ignored = brief(f"{type(exc).__name__}: {exc}", 500)
            logger.error(
                "config_overrides_ignored",
                error=self.overrides_ignored,
                overrides=sorted(config.overrides),
            )
            config = load_config(with_overrides=False)
            boot = _Boot.from_config(config)
            assembly = await self._assemble(config)
        self._boot = boot
        self._install(assembly)
        self._boot_files = {name: self.config.get(name) for name in RESTART_ONLY}
        if self.llm is not None:
            logger.info("llm_enabled", base_url=self.llm.settings.base_url)

    async def _health(self, _request: web.Request) -> web.Response:
        """健康检查。进程活着不算数：和 QQ 的连接断了、又连不回来，机器人就是聋的，要报出来。"""
        ok, problem = self.gateway.health() if self.gateway is not None else (True, "")
        body: dict[str, Any] = {"ok": ok, "service": "jianshanagent"}
        if not ok:
            body["problem"] = problem
        return web.json_response(body, status=200 if ok else 503)

    def build_app(self) -> web.Application:
        """HTTP 服务：健康检查；开了管理后台的话，它也挂在上面。"""
        app = web.Application(client_max_size=_MAX_REQUEST_BYTES)
        app.router.add_get(self._boot.health_path, self._health)
        if self.config.get("core.admin").get("enabled", False):
            from jianshanagent.admin import setup_admin

            setup_admin(app, self)
        return app

    async def serve(self) -> None:
        """开始对外：监听 HTTP 端口，连 QQ。"""
        boot = self._boot
        # 不记访问日志：管理后台一直在轮询，记下来的话日志里全是它自己。
        # 退出时不等没断开的连接太久（网页对话的事件流）：容器只给 30 秒，后面还要存记录、关数据库
        self._web = web.AppRunner(
            self.build_app(), access_log=None, shutdown_timeout=_WEB_SHUTDOWN_SECONDS
        )
        await self._web.setup()
        await web.TCPSite(self._web, host=boot.health_host, port=boot.health_port).start()
        logger.info(
            "healthcheck_listening",
            host=boot.health_host,
            port=boot.health_port,
            path=boot.health_path,
        )

        if self.dry_run:
            logger.warning("dry_run_enabled_skip_qq_gateway")
        elif not boot.gateway_enabled:
            logger.warning("qq_gateway_disabled_by_config")
        else:
            app_id, secret = self.config.qq_credentials
            self.gateway = QQGateway(
                app_id=app_id,
                client_secret=secret,
                on_message=self.handle,
                on_sent=self.sent,
                session_store_dir=boot.session_store_dir,
                max_replies=boot.max_replies,
                drain_seconds=boot.drain_seconds,
            )
            await self.gateway.start()

        if self.trace_store is not None:
            self._maintenance = asyncio.get_running_loop().create_task(self._prune_forever())
        # 走到这里就是起来了：刚装上的更新包从这一刻起算数（起不来的话引导会退回原来的版本）
        self.updater.started()
        self._update_check = asyncio.get_running_loop().create_task(self.updater.run_forever())
        logger.info(
            "jianshanagent_ready",
            dry_run=self.dry_run,
            version=self.updater.current,
            plugins=[p.id for p in self.host.loaded],
        )

    async def handle(self, message: InboundMessage) -> OutboundMessage | None:
        """一条消息交给现在在用的那套路由。网关始终调这里，所以重新装配后它不用知道。"""
        return await self.router.handle(message)

    async def sent(self, inbound: InboundMessage, outbound: OutboundMessage) -> None:
        """网关把最终回复发出去以后通知一声，转给现在在用的那套路由（记群聊记录的插件要看）。"""
        await self.router.sent(inbound, outbound)

    async def _prune_forever(self) -> None:
        """定期清理过期的处理记录（保留多久在 core.trace.yaml）。"""
        store = self.trace_store
        assert store is not None
        while True:
            cfg = self.config.get("core.trace")
            try:
                cleared, removed = await store.prune(
                    prompt_days=int((cfg.get("prompts") or {}).get("retention_days") or 0),
                    trace_days=int(cfg.get("retention_days") or 0),
                )
                if cleared or removed:
                    logger.info("traces_pruned", prompts_cleared=cleared, traces_removed=removed)
            except Exception:
                logger.exception("traces_prune_failed")
            await asyncio.sleep(_PRUNE_EVERY_SECONDS)

    def request_stop(self, *, restart: bool = False) -> None:
        """让进程停下来；``restart`` 为真时停完原地重新启动（见 main.py）。"""
        self.restart_requested = self.restart_requested or restart
        self._stop.set()

    async def wait(self) -> None:
        await self._stop.wait()

    async def stop(self) -> None:
        if self.gateway is not None:
            await self.gateway.stop()
        if self._web is not None:
            await self._web.cleanup()
        for task in [self._maintenance, self._update_check, *self._retiring]:
            if task is not None:
                task.cancel()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self.tracer.drain(), 5)  # 没存完的处理记录存完再走
        shared = self._shared()
        for host in [*self._retired, self.host]:
            await _close_services(host, shared)
        if self.db is not None:
            await self.db.close()
        logger.info("jianshanagent_stopped", restart=self.restart_requested)
