"""配置、模型、插件：在网页里看和改。

改动存成覆写文件（见 core/config.py），不动随发布走的默认值。存完马上重新装配让它生效；
装不起来（写错了）就把覆写文件恢复原样、如实报错，机器人仍按原来的配置运行。
少数几份配置（``RESTART_ONLY``）管的是数据库、QQ 连接、HTTP 服务这些不重装的东西，
存了以后要重启机器人才生效。凭据（secrets.yaml）不在这里，网页上看不到也改不了。
"""

from __future__ import annotations

import ast
import asyncio
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from aiohttp import web

from jianshanagent.admin.core import Admin, fail, read_body, reply
from jianshanagent.admin.schema import build_schema, count_leaves, read_comments
from jianshanagent.core import trace
from jianshanagent.core.config import (
    AppConfig,
    diff_overrides,
    is_locked,
    locked_changes,
    locked_rules,
    restore_override,
    write_override,
)
from jianshanagent.core.llm import LLMError, user_message
from jianshanagent.core.logging import get_logger
from jianshanagent.core.manifest import feature_manifests
from jianshanagent.runtime import RESTART_ONLY

logger = get_logger(__name__)

_GROUPS = (
    ("app", "全局"),
    ("gateway.", "接入"),
    ("core.", "内核"),
    ("plugin.", "插件"),
    ("site.", "站点"),  # 站点插件自己的配置（core.wiki 的 site 用到的那一串才生效）
)
_TEST_PROMPT = "这是一次连通性测试。请只回复两个字：收到"
_TEST_TIMEOUT_SECONDS = 90.0
#: 查证工作流里各个角色用哪一档模型，写在 plugin.research 的哪里
_RESEARCH_ROLES = (
    (
        "analysis",
        "分析问题",
        "查证前归类、摘名字、换算时间。要快，不开思考",
        ("profiles", "analysis"),
    ),
    ("analyst", "查证员", "看资料够不够、写结论", ("profiles", "analyst")),
    ("reviewer", "复核员", "对着资料逐句核对结论", ("profiles", "reviewer")),
    (
        "quick",
        "兜底和快速修正",
        "带思考的调用没成时再做一次；按复核意见只改被点名的地方",
        ("profiles", "quick"),
    ),
)


def _group(name: str) -> str:
    if name.startswith("persona/"):
        return "人设"
    for prefix, label in _GROUPS:
        if name == prefix or (prefix.endswith(".") and name.startswith(prefix)):
            return label
    return "其它"


def _order(name: str) -> tuple[int, str]:
    labels = [label for _, label in _GROUPS] + ["人设", "其它"]
    return labels.index(_group(name)), name


def _source(config: AppConfig, name: str) -> str:
    path = config.source_path(name)
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def _mode(name: str) -> str:
    return "restart" if name in RESTART_ONLY else "reload"


def _dig(node: Any, path: tuple[str, ...]) -> Any:
    for key in path:
        node = node.get(key) if isinstance(node, dict) else None
    return node


async def save_configs(admin: Admin, edits: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """把改完的几份配置存成覆写，并让它们生效。

    几份一起存、一起生效：要么都成，要么都撤回（改模型档位的名字要同时动两份配置）。
    """
    disk = admin.disk_config()
    if disk.overrides_dir is None:
        fail(409, "没有配置覆写目录（环境变量 JIANSHANAGENT_CONFIG_OVERRIDES_DIR），改动没有地方存")
    for name, values in edits.items():
        if name not in disk.base_files:
            fail(404, f"没有叫「{name}」的配置")
        if not isinstance(values, dict):
            fail(400, f"「{name}」的内容应该是键值结构")
        locked = locked_changes(name, diff_overrides(disk.base_files[name], values))
        if locked:
            fail(
                400,
                f"「{name}」里的 {'、'.join(locked)} 不能在网页上改"
                "（接口地址、用哪个密钥、管理后台和健康检查的开关：要改就改仓库里的配置文件再发布）",
            )
    previous: dict[str, str | None] = {}
    for name in edits:
        path = disk.override_path(name)
        previous[name] = path.read_text(encoding="utf-8") if path and path.is_file() else None

    def undo() -> None:
        for name, text in previous.items():
            restore_override(disk, name, text)

    try:
        for name, values in edits.items():
            write_override(disk, name, values)
    except Exception:
        undo()
        raise
    changed = sorted(edits)
    if any(name not in RESTART_ONLY for name in edits):
        try:
            await admin.runtime.reload()
        except Exception as exc:
            undo()
            logger.warning("admin_config_rejected", files=changed, error=trace.brief(repr(exc)))
            fail(
                409,
                "按新配置装不起来，改动已撤回，机器人仍按原来的配置运行："
                + trace.brief(f"{type(exc).__name__}: {exc}", 500),
            )
    logger.info("admin_config_saved", files=changed)
    needs_restart = [name for name in changed if name in RESTART_ONLY]
    return {"saved": changed, "needs_restart": needs_restart, "status": admin.status()}


def _catalog(workspace_root: Path | None = None) -> dict[str, str]:
    """代码里有哪些功能插件 → 各自的说明（插件模块开头的文档字符串或清单说明，不导入模块）。"""
    found: dict[str, str] = {}
    for plugin_id, manifest in feature_manifests(workspace_root).items():
        folder = manifest.path
        doc = ""
        for candidate in (folder / "plugin.py", folder / "__init__.py"):
            if candidate.is_file():
                try:
                    doc = ast.get_docstring(ast.parse(candidate.read_text("utf-8"))) or ""
                except (SyntaxError, OSError):
                    doc = ""
                if doc:
                    break
        found[plugin_id] = doc or manifest.description
    return found


def routes(admin: Admin) -> list[web.RouteDef]:
    def describe(disk: AppConfig, name: str) -> dict[str, Any]:
        header, _ = read_comments(_source(disk, name))
        return {
            "name": name,
            "group": _group(name),
            "summary": header.split("\n", 1)[0],
            "mode": _mode(name),
            "overridden": count_leaves(disk.overrides.get(name, {})),
            "error": disk.override_errors.get(name),
        }

    async def list_config(_request: web.Request) -> web.Response:
        disk = admin.disk_config()
        files = [describe(disk, name) for name in sorted(disk.base_files, key=_order)]
        return reply({"files": files, "state": admin.status()["config"]})

    async def get_config(request: web.Request) -> web.Response:
        name = request.match_info["name"]
        disk = admin.disk_config()
        if name not in disk.base_files:
            fail(404, f"没有叫「{name}」的配置")
        source = _source(disk, name)
        header, notes = read_comments(source)
        base, effective = disk.base_files[name], disk.files[name]
        path = disk.override_path(name)
        return reply(
            {
                **describe(disk, name),
                "description": header,
                "schema": build_schema(base, effective, notes, name=name),
                # 哪些项不许在网页上改（表单结构里每一项也各带一个 locked）：页面照着显示成只读，
                # 这次编辑里新加出来的项也按它判断
                "locked": locked_rules(name),
                "base": base,
                "effective": effective,
                "override": disk.overrides.get(name, {}),
                "source": source,
                "override_text": path.read_text("utf-8") if path and path.is_file() else "",
                "state": admin.status()["config"],
            }
        )

    async def put_config(request: web.Request) -> web.Response:
        name = request.match_info["name"]
        body = await read_body(request)
        values = body.get("values")
        if not isinstance(values, dict):
            fail(400, "values 应该是键值结构")
        return reply(await save_configs(admin, {name: values}))

    async def put_configs(request: web.Request) -> web.Response:
        body = await read_body(request)
        files = body.get("files")
        if not isinstance(files, dict) or not files:
            fail(400, "没有要保存的配置")
        return reply(await save_configs(admin, files))

    # ---- 模型 ----

    async def models(_request: web.Request) -> web.Response:
        disk = admin.disk_config()
        llm = disk.files.get("core.llm", {})
        research = disk.files.get("plugin.research", {})
        _, notes = read_comments(_source(disk, "core.llm"))
        base_profiles = (disk.base_files.get("core.llm", {}).get("profiles")) or {}
        usage: dict[str, dict[str, Any]] = {}
        if admin.has_database:
            since = datetime.now(admin.timezone) - timedelta(hours=24)
            usage = await admin.queries.profile_usage(since)
        roles: list[dict[str, Any]] = [
            {
                "id": "entry",
                "label": "对话入口",
                "hint": "判断一句话是直接回，还是交给查证；闲聊也是它回",
                "profile": "chat",
                "file": None,
                "path": [],
            }
        ]
        for role, label, hint, path in _RESEARCH_ROLES:
            roles.append(
                {
                    "id": role,
                    "label": label,
                    "hint": hint,
                    "profile": str(_dig(research, path) or ""),
                    "file": "plugin.research",
                    "path": list(path),
                }
            )
        running = admin.runtime.llm
        return reply(
            {
                "enabled": bool(llm.get("enabled", False)),
                "running": running is not None,
                "base_url": llm.get("base_url"),
                # 接口地址不许在网页上改（密钥是发往这个地址的）：页面上只显示，注明要改配置文件
                "base_url_locked": is_locked("core.llm", ("base_url",)),
                "default_profile": llm.get("default_profile"),
                "timeout_seconds": llm.get("timeout_seconds"),
                # 联网搜索不用这里的档位，另有地址和密钥（core.websearch.yaml）；这里只说开没开
                "web_search": bool(disk.files.get("core.websearch", {}).get("enabled", False)),
                "profiles": [
                    {
                        "name": name,
                        "model": item.get("model"),
                        "thinking": bool(item.get("thinking", False)),
                        "max_completion_tokens": item.get("max_completion_tokens"),
                        "temperature": item.get("temperature"),
                        "description": notes.get(("profiles", name), ""),
                        "added": name not in base_profiles,
                        "usage": usage.get(name),
                    }
                    for name, item in (llm.get("profiles") or {}).items()
                    if isinstance(item, dict)
                ],
                "roles": roles,
                "llm": llm,
                "research": research,
                "state": admin.status()["config"],
            }
        )

    async def test_model(request: web.Request) -> web.Response:
        body = await read_body(request)
        name = str(body.get("profile") or "")
        llm = admin.runtime.llm
        if llm is None:
            fail(409, "模型没有启用（core.llm.yaml 的 enabled）")
        if name not in llm.settings.profiles:
            fail(404, f"正在运行的配置里没有「{name}」这一档（改动要先保存）")
        started = time.perf_counter()
        try:
            with trace.purpose("test"):
                result = await asyncio.wait_for(
                    llm.chat([user_message(_TEST_PROMPT)], profile=name), _TEST_TIMEOUT_SECONDS
                )
        except TimeoutError:
            return reply(
                {
                    "ok": False,
                    "seconds": time.perf_counter() - started,
                    "error": f"{_TEST_TIMEOUT_SECONDS:.0f} 秒没有回应",
                }
            )
        except LLMError as exc:
            return reply(
                {"ok": False, "seconds": time.perf_counter() - started, "error": str(exc)[:500]}
            )
        usage = result.usage
        return reply(
            {
                "ok": True,
                "seconds": time.perf_counter() - started,
                "model": result.model,
                "finish_reason": result.finish_reason,
                "content": trace.brief(result.content, 500),
                "reasoning_chars": len(result.reasoning),
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
                "reasoning_tokens": usage.reasoning_tokens,
            }
        )

    # ---- 插件 ----

    async def plugins(_request: web.Request) -> web.Response:
        disk = admin.disk_config()
        host = admin.host
        enabled = [str(item) for item in disk.files.get("core.plugins", {}).get("enabled") or []]
        default = [
            str(item) for item in disk.base_files.get("core.plugins", {}).get("enabled") or []
        ]
        loaded = {plugin.id: plugin for plugin in host.loaded}
        catalog = _catalog(disk.root.parent)
        items: list[dict[str, Any]] = []
        for plugin_id in [*enabled, *(name for name in catalog if name not in enabled)]:
            doc = catalog.get(plugin_id, "")
            entry: dict[str, Any] = {
                "id": plugin_id,
                "exists": plugin_id in catalog,
                "enabled": plugin_id in enabled,
                "loaded": plugin_id in loaded,
                "summary": doc.split("\n", 1)[0],
                "description": doc,
                "config": f"plugin.{plugin_id}"
                if f"plugin.{plugin_id}" in disk.base_files
                else None,
                "commands": [],
                "tools": [],
                "keywords": [],
                "services": [],
                "hooks": {},
                "active": False,
            }
            plugin = loaded.get(plugin_id)
            if plugin is not None:
                context = plugin.context
                entry["commands"] = [
                    {
                        "name": command.name,
                        "aliases": command.aliases,
                        "description": command.description,
                    }
                    for command in context.commands
                ]
                entry["tools"] = [
                    {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters,
                    }
                    for tool in host.tools.values()
                    if tool.plugin_id == plugin_id
                ]
                entry["keywords"] = [keyword.pattern for keyword in context.keywords]
                entry["services"] = list(context.service_names)
                entry["hooks"] = {
                    "chat": context.chat_handler is not None,
                    "inbound": len(context.inbound_middlewares),
                    "outbound": len(context.outbound_filters),
                    "observers": len(context.observers),
                }
                entry["active"] = bool(
                    entry["commands"]
                    or entry["tools"]
                    or entry["keywords"]
                    or entry["services"]
                    or any(entry["hooks"].values())
                )
            items.append(entry)
        return reply(
            {
                "enabled": enabled,
                "default": default,
                "plugins": items,
                "state": admin.status()["config"],
            }
        )

    async def put_plugins(request: web.Request) -> web.Response:
        body = await read_body(request)
        wanted = body.get("enabled")
        if not isinstance(wanted, list) or not all(isinstance(item, str) for item in wanted):
            fail(400, "enabled 应该是插件名的列表")
        disk = admin.disk_config()
        catalog = _catalog(disk.root.parent)
        unknown = [item for item in wanted if item not in catalog]
        if unknown:
            fail(400, f"没有这些插件：{'、'.join(unknown)}")
        if len(set(wanted)) != len(wanted):
            fail(400, "插件名有重复")
        edited = {**disk.files.get("core.plugins", {}), "enabled": wanted}
        return reply(await save_configs(admin, {"core.plugins": edited}))

    return [
        web.get("/api/config", list_config),
        web.put("/api/config", put_configs),
        web.get("/api/config/{name:.+}", get_config),
        web.put("/api/config/{name:.+}", put_config),
        web.get("/api/models", models),
        web.post("/api/models/test", test_model),
        web.get("/api/plugins", plugins),
        web.put("/api/plugins", put_plugins),
    ]
