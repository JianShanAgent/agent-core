from __future__ import annotations

import importlib
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from jianshanagent.core import trace
from jianshanagent.core.config import AppConfig
from jianshanagent.core.logging import get_logger
from jianshanagent.core.manifest import Manifest, PluginError, feature_manifests
from jianshanagent.core.messages import InboundMessage, OutboundMessage

logger = get_logger(__name__)

CommandHandler = Callable[[InboundMessage, str], Awaitable[OutboundMessage | None]]
KeywordHandler = Callable[[InboundMessage], Awaitable[OutboundMessage | None]]
Middleware = Callable[[InboundMessage], Awaitable[InboundMessage | None]]
#: 旁听：网关收到的每一条消息都给它看一眼（包括不是对机器人说的），它不能改消息，也不回复
Observer = Callable[[InboundMessage], Awaitable[None]]
#: 旁听机器人发出去的回复：（回复的是哪条消息，发出去的内容）。只有最终的回复，进度消息不算
ReplyObserver = Callable[[InboundMessage, OutboundMessage], Awaitable[None]]
#: 删掉一个用户留在某个插件里的数据，返回删了几条。
#: 参数是他提出要求的那条消息：从它知道是谁、在哪个会话
Eraser = Callable[[InboundMessage], Awaitable[int]]
OutboundFilter = Callable[[OutboundMessage, InboundMessage], Awaitable[OutboundMessage | None]]


@dataclass
class CommandSpec:
    name: str
    handler: CommandHandler
    aliases: list[str] = field(default_factory=list)
    description: str = ""
    plugin_id: str = ""


#: 参数是（模型给的参数，触发这次调用的消息）；消息用来确定是哪个用户、哪个会话
ToolHandler = Callable[[dict[str, Any], InboundMessage], Awaitable[Any]]
ChatHandler = Callable[[InboundMessage], Awaitable[OutboundMessage | None]]


@dataclass
class ToolSpec:
    """供 LLM 调用的工具：``parameters`` 是 JSON Schema，``handler`` 返回可序列化为 JSON 的结果。"""

    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler
    plugin_id: str = ""


@dataclass
class KeywordSpec:
    pattern: str
    handler: KeywordHandler
    plugin_id: str = ""
    priority: int = 100


@dataclass
class EraserSpec:
    #: 删的是什么，给用户看的说法（「记忆」「群聊记录」）
    label: str
    handler: Eraser
    plugin_id: str = ""


async def erase_user_data(
    erasers: list[EraserSpec], message: InboundMessage
) -> tuple[dict[str, int], list[str]]:
    """用户要求删除他的数据时：把他留在各个插件里的都删掉。

    返回（每一类删了几条，没删成的是哪几类）。某一类删不成不影响别的，但要如实告诉用户。
    范围是这个人：他在所有群里留下的都删，不只是他提出要求的这个群。
    提出要求的这条消息自己的处理记录也不留（见 ``trace.forget_current``）。
    """
    trace.forget_current()
    deleted: dict[str, int] = {}
    failed: list[str] = []
    for spec in erasers:
        try:
            deleted[spec.label] = deleted.get(spec.label, 0) + await spec.handler(message)
        except Exception:
            logger.exception("erase_failed", plugin=spec.plugin_id, what=spec.label)
            failed.append(spec.label)
    return deleted, failed


class PluginContext:
    """插件注册期拿到的宿主能力。"""

    def __init__(
        self,
        plugin_id: str,
        config: AppConfig,
        services: dict[str, Any],
        tools: dict[str, ToolSpec] | None = None,
        erasers: list[EraserSpec] | None = None,
    ):
        self.plugin_id = plugin_id
        self.config = config
        self.services = services
        #: 所有插件共享的工具表；后加载的插件注册的工具，先加载的插件在调用时也能看到
        self.tools: dict[str, ToolSpec] = tools if tools is not None else {}
        #: 所有插件共享：各个插件存了用户数据的，在这里登记怎么删（见 :func:`erase_user_data`）。
        #: 和工具表一样，后加载的插件登记的，先加载的插件在调用时也能看到
        self.erasers: list[EraserSpec] = erasers if erasers is not None else []
        self.chat_handler: ChatHandler | None = None
        self.commands: list[CommandSpec] = []
        self.keywords: list[KeywordSpec] = []
        self.inbound_middlewares: list[Middleware] = []
        self.outbound_filters: list[OutboundFilter] = []
        self.observers: list[Observer] = []
        self.reply_observers: list[ReplyObserver] = []
        #: 这个插件注册了哪些服务（管理后台的插件一览用）
        self.service_names: list[str] = []

    def plugin_config(self) -> dict[str, Any]:
        return self.config.plugin(self.plugin_id)

    def add_command(
        self,
        name: str,
        handler: CommandHandler,
        *,
        aliases: list[str] | None = None,
        description: str = "",
    ) -> None:
        self.commands.append(
            CommandSpec(
                name=name,
                handler=handler,
                aliases=list(aliases or []),
                description=description,
                plugin_id=self.plugin_id,
            )
        )

    def add_keyword(
        self,
        pattern: str,
        handler: KeywordHandler,
        *,
        priority: int = 100,
    ) -> None:
        self.keywords.append(
            KeywordSpec(
                pattern=pattern,
                handler=handler,
                plugin_id=self.plugin_id,
                priority=priority,
            )
        )

    def add_inbound_middleware(self, mw: Middleware) -> None:
        self.inbound_middlewares.append(mw)

    def add_outbound_filter(self, filt: OutboundFilter) -> None:
        self.outbound_filters.append(filt)

    def add_observer(self, observer: Observer) -> None:
        """旁听收到的每一条消息（包括不是对机器人说的那些）。出错不影响机器人回复。"""
        self.observers.append(observer)

    def add_reply_observer(self, observer: ReplyObserver) -> None:
        """旁听机器人发出去的每一条最终回复。出错不影响别的。"""
        self.reply_observers.append(observer)

    def add_eraser(self, label: str, eraser: Eraser) -> None:
        """登记怎么删一个用户留在本插件里的数据。平台的运营规范要求用户提出时必须能删：
        存了用户数据的插件都要登记。``label`` 是删的东西给用户看的说法。"""
        self.erasers.append(EraserSpec(label, eraser, self.plugin_id))

    def register_service(self, name: str, service: Any) -> None:
        self.services[name] = service
        self.service_names.append(name)

    def add_tool(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        handler: ToolHandler,
    ) -> None:
        if name in self.tools:
            raise RuntimeError(
                f"工具冲突: {name}（{self.tools[name].plugin_id} vs {self.plugin_id}）"
            )
        self.tools[name] = ToolSpec(name, description, parameters, handler, self.plugin_id)

    def set_chat_handler(self, handler: ChatHandler) -> None:
        """接管「没有命中任何命令」的消息（自然语言）。"""
        self.chat_handler = handler


class Plugin(Protocol):
    id: str

    def register(self, ctx: PluginContext) -> None: ...


@dataclass
class LoadedPlugin:
    id: str
    #: 这个插件的清单（名字、说明、开了哪些扩展点）
    manifest: Manifest
    instance: Any
    context: PluginContext


class PluginHost:
    """按 core.plugins.yaml 加载插件并汇总注册表。"""

    def __init__(self, config: AppConfig):
        self.config = config
        self.services: dict[str, Any] = {}
        self.tools: dict[str, ToolSpec] = {}
        self.chat_handler: ChatHandler | None = None
        self.loaded: list[LoadedPlugin] = []
        self.commands: dict[str, CommandSpec] = {}
        self.keywords: list[KeywordSpec] = []
        self.inbound_middlewares: list[Middleware] = []
        self.outbound_filters: list[OutboundFilter] = []
        self.observers: list[Observer] = []
        self.reply_observers: list[ReplyObserver] = []
        self.erasers: list[EraserSpec] = []

    def load(self) -> None:
        """按 ``core.plugins.yaml`` 的 ``enabled`` 依次装功能插件（规范见 ``core/manifest.py``）：
        每个插件要有清单，包里要有 ``create_plugin()``，返回的对象有 ``id`` 和同步的
        ``register(ctx)``。"""
        enabled = [str(name) for name in self.config.get("core.plugins").get("enabled") or []]
        manifests = feature_manifests(self.config.root.parent)
        for position, plugin_id in enumerate(enabled):
            manifest = manifests.get(plugin_id)
            if manifest is None:
                logger.error("plugin_missing", plugin_id=plugin_id)
                raise PluginError(
                    f"没有叫「{plugin_id}」的功能插件（有：{'、'.join(manifests)}）："
                    "插件目录里要有清单 plugin.yaml"
                )
            # 清单里写了要排在谁后面：那个插件也启用了、却排在它后面的话，它多半用不上对方注册的东西
            late = [name for name in manifest.after if name in enabled[position + 1 :]]
            if late:
                logger.warning("plugin_order", plugin_id=plugin_id, should_come_after=late)
            module = importlib.import_module(manifest.package)
            factory = getattr(module, "create_plugin", None)
            if not callable(factory):
                raise PluginError(f"功能插件「{plugin_id}」的包里没有 create_plugin()")
            plugin_obj = factory()
            if getattr(plugin_obj, "id", None) != plugin_id:
                raise PluginError(
                    f"功能插件「{plugin_id}」的 create_plugin() 返回的对象 id 是"
                    f"「{getattr(plugin_obj, 'id', None)}」，要和清单里的 id 一样"
                )
            ctx = PluginContext(plugin_id, self.config, self.services, self.tools, self.erasers)
            if inspect.iscoroutinefunction(plugin_obj.register):
                raise TypeError(f"插件 {plugin_id}.register 必须是同步方法")
            plugin_obj.register(ctx)
            self.loaded.append(
                LoadedPlugin(id=plugin_id, manifest=manifest, instance=plugin_obj, context=ctx)
            )
            for cmd in ctx.commands:
                keys = [cmd.name, *cmd.aliases]
                for key in keys:
                    normalized = key.lstrip("/").strip().lower()
                    if normalized in self.commands:
                        raise RuntimeError(
                            f"命令冲突: {normalized} "
                            f"({self.commands[normalized].plugin_id} vs {plugin_id})"
                        )
                    self.commands[normalized] = cmd
            if ctx.chat_handler is not None:
                self.chat_handler = ctx.chat_handler
            self.keywords.extend(ctx.keywords)
            self.inbound_middlewares.extend(ctx.inbound_middlewares)
            self.outbound_filters.extend(ctx.outbound_filters)
            self.observers.extend(ctx.observers)
            self.reply_observers.extend(ctx.reply_observers)
            logger.info(
                "plugin_loaded",
                plugin_id=plugin_id,
                commands=len(ctx.commands),
                keywords=len(ctx.keywords),
            )
        self.keywords.sort(key=lambda k: k.priority)

    def get_service(self, name: str) -> Any:
        return self.services[name]
