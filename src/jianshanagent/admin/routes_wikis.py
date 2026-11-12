"""挂着的 wiki：看现状，从站点目录里添加、移除，给新挂上的建知识库。

部署的配置文件（``core.wiki.yaml`` 的 ``wikis``）里写的 wiki 在这里只能看；在这里加的存在
覆写目录里（``wikis_added``，见 core/config.py），接在它们后面。能加的只有站点目录里的站
（见 core/catalog.py）：地址、用哪个站点插件都照目录里写的，网页上只能给它起名字——
不然登录了后台的人就能让机器人把放行密钥发到别的地址、加载别的代码。

添加：环境里没有那个站点插件就先下载，然后重新装配，``/查`` 马上能用。装不起来就撤回。
知识库不跟着建（要全量取数、算向量，时间和花费都不小）：另点「建知识库」，由自动更新（kbsync）
接手，见 plugins/knowledge/sync.py 的「从头建」。
"""

from __future__ import annotations

import asyncio
import re
from importlib import metadata
from pathlib import Path
from typing import Any

from aiohttp import web

from jianshanagent.admin.core import Admin, fail, flag_param, read_body, reply
from jianshanagent.core import trace
from jianshanagent.core.catalog import CatalogError, CatalogSettings, uninstall
from jianshanagent.core.config import (
    WIKI_FILE,
    AppConfig,
    WikiEntry,
    added_wikis,
    restore_override,
    write_added_wikis,
)
from jianshanagent.core.embedding import EmbeddingSettings
from jianshanagent.core.logging import get_logger
from jianshanagent.core.manifest import (
    Manifest,
    downloaded_sites_dir,
    installed_sites,
    platforms,
    read_sites_state,
    site_source,
)
from jianshanagent.plugins.knowledge.settings import kb_root, load_settings
from jianshanagent.plugins.knowledge.sync import (
    KB_NONE,
    cancel_build,
    healthy,
    kb_state,
    request_build,
)

logger = get_logger(__name__)

_WIKI_ID = re.compile(r"[a-z][a-z0-9_]{0,31}")
_MAX_LABEL_CHARS = 40
_SOURCE_DOWNLOADED = "downloaded"


def _workspace(disk: AppConfig) -> Path:
    return disk.root.parent


def _site_info(disk: AppConfig, manifest: Manifest) -> dict[str, Any]:
    """一个站点插件：叫什么、哪来的、哪个版本（查不出来是 None）。"""
    root = _workspace(disk)
    source = site_source(manifest, root)
    version: str | None = None
    if source == _SOURCE_DOWNLOADED:
        directory = downloaded_sites_dir(root)
        state = read_sites_state(directory) if directory is not None else {}
        version = str((state.get(manifest.id) or {}).get("version") or "") or None
    elif source == "environment":
        try:
            version = metadata.version(manifest.package)
        except metadata.PackageNotFoundError:
            version = None
    return {"id": manifest.id, "name": manifest.name, "source": source, "version": version}


def _secret_set(disk: AppConfig, wiki_id: str) -> bool:
    """``secrets.yaml`` 里这个 wiki 的放行密钥填了没有。只看填没填，值不出这个函数。"""
    value = str(((disk.secrets.get("wiki") or {}).get(wiki_id) or {}).get("authkey") or "").strip()
    return bool(value) and not value.upper().startswith("YOUR_")


def _kb(disk: AppConfig, wiki_id: str) -> dict[str, Any]:
    try:
        return kb_state(kb_root(disk) / wiki_id)
    except (ValueError, OSError) as exc:
        return {"state": KB_NONE, "requested_at": None, "started_at": None, "problem": str(exc)}


def routes(admin: Admin) -> list[web.RouteDef]:
    #: 添加、移除、更新一次只做一个
    changing = asyncio.Lock()

    def describe(disk: AppConfig, entry: WikiEntry) -> dict[str, Any]:
        raw = entry.raw
        return {
            "id": entry.id,
            "label": str(raw.get("label") or "").strip() or entry.id,
            "primary": entry.id == disk.wikis[0].id,
            #: 在这里加的（能在这里移除）；不然是部署的配置文件里写的
            "added": entry.added,
            "platform": entry.platform.id,
            "site": _site_info(disk, entry.site) if entry.site is not None else None,
            "api_url": str(raw.get("api_url") or ""),
            "auth_header": str(raw.get("auth_header") or ""),
            "auth_key_set": _secret_set(disk, entry.id),
            #: 正在运行的那一套里有没有它（刚加上、还要重启才生效的是 False）
            "running": any(running.id == entry.id for running in admin.config.wikis),
            "kb": _kb(disk, entry.id),
        }

    def kb_overview(disk: AppConfig) -> dict[str, Any]:
        """建知识库靠的几样东西在不在：知识库插件、嵌入接口、自动更新服务。"""
        enabled = [str(item) for item in disk.files.get("core.plugins", {}).get("enabled") or []]
        try:
            alive = healthy(kb_root(disk))
        except ValueError:
            alive = False
        return {
            "plugin": "knowledge" in enabled,
            "embedding": EmbeddingSettings.from_config(disk) is not None,
            "sync_alive": alive,
        }

    async def list_wikis(_request: web.Request) -> web.Response:
        disk = admin.disk_config()
        try:
            settings = CatalogSettings.from_config(disk)
            catalog = {"enabled": settings.enabled, "repo": settings.repo}
        except ValueError as exc:
            catalog = {"enabled": False, "repo": "", "error": str(exc)}
        return reply(
            {
                "writable": disk.overrides_dir is not None,
                "platforms": [
                    {"id": item.id, "name": item.name, "description": item.description}
                    for item in platforms().values()
                ],
                "wikis": [describe(disk, entry) for entry in disk.wikis],
                #: 在这里加过、现在挂不上的（它的站点插件没了……）和原因
                "skipped": disk.override_errors.get(WIKI_FILE, ""),
                "kb": kb_overview(disk),
                "catalog": catalog,
                "state": admin.status()["config"],
            }
        )

    async def list_catalog(request: web.Request) -> web.Response:
        """站点目录里的站。要联网读，所以和上面那个分开：读不成不耽误看已经挂着的。"""
        disk = admin.disk_config()
        catalog = admin.runtime.catalog
        try:
            settings = catalog.settings()
            sites = await catalog.load(refresh=flag_param(request, "refresh"))
        except (CatalogError, ValueError) as exc:
            return reply({"enabled": True, "repo": "", "sites": [], "error": str(exc)})
        installed = installed_sites(_workspace(disk))
        used = {entry.site.id: entry.id for entry in disk.wikis if entry.site is not None}
        return reply(
            {
                "enabled": settings.enabled,
                "repo": settings.repo,
                "sites": [
                    {
                        "id": site.id,
                        "platform": site.platform,
                        "name": site.name,
                        "description": site.description,
                        "repo": site.repo,
                        "wiki_id": site.wiki_id,
                        "label": site.label,
                        "api_url": site.wiki.get("api_url", ""),
                        "auth_header": site.auth_header,
                        "unavailable": site.unavailable,
                        #: 这个站点插件在不在这台机器上；不在的话添加时会下载
                        "installed": _site_info(disk, installed[site.id])
                        if site.id in installed
                        else None,
                        #: 已经挂着的话，是哪个 wiki
                        "wiki": used.get(site.id),
                    }
                    for site in sites
                ],
                "error": catalog.error,
                "problems": catalog.problems,
                "fetched_at": catalog.fetched_at,
            }
        )

    async def reload_or_undo(disk: AppConfig, previous: str | None, what: str) -> None:
        """重新装配；装不起来就把覆写文件恢复原样、再装回去，然后报错。"""
        try:
            await admin.runtime.reload()
        except Exception as exc:
            restore_override(disk, WIKI_FILE, previous)
            logger.warning("admin_wiki_change_rejected", what=what, error=trace.brief(repr(exc)))
            fail(
                409,
                f"{what}以后装不起来，已经撤回，机器人仍按原来的配置运行："
                + trace.brief(f"{type(exc).__name__}: {exc}", 500),
            )

    def override_text(disk: AppConfig) -> str | None:
        path = disk.override_path(WIKI_FILE)
        return path.read_text(encoding="utf-8") if path is not None and path.is_file() else None

    async def add_wiki(request: web.Request) -> web.Response:
        body = await read_body(request)
        site_id = str(body.get("site") or "")
        async with changing:
            disk = admin.disk_config()
            if disk.overrides_dir is None:
                fail(
                    409,
                    "没有配置覆写目录（环境变量 JIANSHANAGENT_CONFIG_OVERRIDES_DIR），"
                    "加的 wiki 没有地方存",
                )
            catalog = admin.runtime.catalog
            try:
                site = await catalog.site(site_id)
            except (CatalogError, ValueError) as exc:
                fail(404, str(exc))
            if site.unavailable:
                fail(409, site.unavailable)
            wiki_id = str(body.get("id") or "").strip() or site.wiki_id
            label = " ".join(str(body.get("label") or "").split()) or site.label
            if not _WIKI_ID.fullmatch(wiki_id):
                fail(400, "wiki 的名字要小写字母开头，只用小写字母、数字、下划线，最多 32 个字符")
            if len(label) > _MAX_LABEL_CHARS:
                fail(400, f"出处里的叫法最多 {_MAX_LABEL_CHARS} 个字")
            stored = added_wikis(disk)
            for entry in disk.wikis:
                if entry.site is not None and entry.site.id == site.id:
                    fail(409, f"这个站已经挂着了（wiki「{entry.id}」）")
            taken = {entry.id for entry in disk.wikis} | {str(item.get("id")) for item in stored}
            if wiki_id in taken:
                fail(409, f"已经有一个叫「{wiki_id}」的 wiki 了，换一个名字")

            root = _workspace(disk)
            directory = downloaded_sites_dir(root)
            downloaded: str | None = None
            if site.id not in installed_sites(root):
                if directory is None:
                    fail(409, "不知道把站点插件下载到哪（没有设 JIANSHANAGENT_SITES_DIR）")
                try:
                    downloaded = await catalog.install(site, directory)
                except CatalogError as exc:
                    fail(409, f"站点插件「{site.id}」没装上：{exc}")

            def undo_download() -> None:
                if downloaded is not None and directory is not None:
                    uninstall(directory, site.id)

            previous = override_text(disk)
            try:
                write_added_wikis(disk, [*stored, site.wiki_entry(wiki_id, label)])
                await reload_or_undo(disk, previous, f"加上「{wiki_id}」")
            except BaseException:
                undo_download()
                raise
            skipped = admin.config.override_errors.get(WIKI_FILE, "")
            if not any(entry.id == wiki_id for entry in admin.config.wikis):
                # 配置读得进来、它却没挂上（被跳过了）：照样撤回
                restore_override(disk, WIKI_FILE, previous)
                undo_download()
                await admin.runtime.reload()
                fail(409, f"「{wiki_id}」没有挂上，已经撤回：{skipped or '原因不明'}")
            logger.warning("admin_wiki_added", wiki=wiki_id, site=site.id, downloaded=downloaded)
            fresh = admin.disk_config()
            return reply(
                {
                    "added": describe(fresh, fresh.wiki(wiki_id)),
                    #: 这次下载的站点插件的版本；本来就在这台机器上的是 None
                    "downloaded": downloaded,
                    "status": admin.status(),
                }
            )

    async def remove_wiki(request: web.Request) -> web.Response:
        wiki_id = request.match_info["id"]
        async with changing:
            disk = admin.disk_config()
            stored = added_wikis(disk)
            target = next((item for item in stored if item.get("id") == wiki_id), None)
            if target is None:
                if any(entry.id == wiki_id for entry in disk.wikis):
                    fail(
                        409,
                        f"「{wiki_id}」是部署的配置文件（core.wiki.yaml）里写的，"
                        "不能在这里移除：改那个文件再发布",
                    )
                fail(404, f"没有叫「{wiki_id}」的 wiki")
            rest = [item for item in stored if item is not target]
            previous = override_text(disk)
            write_added_wikis(disk, rest)
            await reload_or_undo(disk, previous, f"移除「{wiki_id}」")
            try:
                cancel_build(kb_root(disk) / wiki_id)
            except (ValueError, OSError):
                pass
            # 它的站点插件是下载的、又没有别的 wiki 用了：一起删掉
            site_id = str(target.get("site") or "")
            still_used = any(
                entry.site is not None and entry.site.id == site_id for entry in admin.config.wikis
            )
            directory = downloaded_sites_dir(_workspace(disk))
            removed_site = bool(
                site_id
                and not still_used
                and directory is not None
                and uninstall(directory, site_id)
            )
            logger.warning("admin_wiki_removed", wiki=wiki_id, site_removed=removed_site)
            return reply(
                {"removed": wiki_id, "site_removed": removed_site, "status": admin.status()}
            )

    async def build_kb(request: web.Request) -> web.Response:
        """请自动更新给这个 wiki 从头建知识库。只放一个请求，不在这个进程里做。"""
        wiki_id = request.match_info["id"]
        disk = admin.disk_config()
        if not any(entry.id == wiki_id for entry in disk.wikis):
            fail(404, f"没有叫「{wiki_id}」的 wiki")
        overview = kb_overview(disk)
        if not overview["plugin"]:
            fail(409, "知识库插件（knowledge）没有启用")
        if not overview["embedding"]:
            fail(409, "没有配置嵌入接口（core.embedding.yaml 的 enabled），建不了知识库")
        try:
            settings = load_settings(disk, wiki_id)
        except ValueError as exc:
            fail(409, f"这个 wiki 的知识库配置读不了：{exc}")
        if not settings.sync.enabled:
            fail(409, "这个 wiki 的自动更新关着（plugin.knowledge 的 sync.enabled），建库靠的是它")
        current = kb_state(settings.output_dir)
        if current["state"] != KB_NONE:
            fail(409, "它已经有知识库了，或者已经在建了")
        request_build(settings.output_dir)
        logger.warning("admin_kb_build_requested", wiki=wiki_id)
        return reply({"kb": kb_state(settings.output_dir), "sync_alive": overview["sync_alive"]})

    async def cancel_kb(request: web.Request) -> web.Response:
        wiki_id = request.match_info["id"]
        disk = admin.disk_config()
        if not any(entry.id == wiki_id for entry in disk.wikis):
            fail(404, f"没有叫「{wiki_id}」的 wiki")
        directory = kb_root(disk) / wiki_id
        if not cancel_build(directory):
            fail(409, "没有可以撤回的请求（没点过，或者已经开始建了）")
        return reply({"kb": kb_state(directory)})

    async def update_site(request: web.Request) -> web.Response:
        """把下载的站点插件换成它的仓库最新的版本，并重启（已经导入的代码要重启才换得掉）。"""
        site_id = request.match_info["site"]
        async with changing:
            disk = admin.disk_config()
            directory = downloaded_sites_dir(_workspace(disk))
            state = read_sites_state(directory) if directory is not None else {}
            if directory is None or site_id not in state:
                fail(409, f"「{site_id}」不是在这里下载的：它随部署走，更新它要重新部署")
            before = str(state[site_id].get("version") or "")
            catalog = admin.runtime.catalog
            try:
                version = await catalog.install(await catalog.site(site_id), directory)
            except (CatalogError, ValueError) as exc:
                fail(409, f"站点插件「{site_id}」没更新成：{exc}")
            updated = version != before
            if updated:
                logger.warning("admin_site_updated", site=site_id, version=version, previous=before)
                admin.restart_soon()
            return reply({"version": version, "updated": updated, "restarting": updated})

    return [
        web.get("/api/wikis", list_wikis),
        web.get("/api/wikis/catalog", list_catalog),
        web.post("/api/wikis", add_wiki),
        web.post("/api/wikis/sites/{site}/update", update_site),
        web.delete("/api/wikis/{id}", remove_wiki),
        web.post("/api/wikis/{id}/kb", build_kb),
        web.delete("/api/wikis/{id}/kb", cancel_kb),
    ]
