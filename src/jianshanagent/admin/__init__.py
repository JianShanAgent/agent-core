"""管理后台：在浏览器里看机器人在干什么、改配置、管数据。

挂在机器人自己的 HTTP 服务上（和健康检查同一个端口、同一个进程），这样才拿得到插件、配置和
正在处理的消息。它不是插件：插件会被重新装配，而它要管插件的启停，所以是进程里长驻的一部分，
每次用到配置、插件、路由都从 ``Runtime`` 上现取。

- 接口都在 ``/api/`` 下面（各个 ``routes_*.py``），除了登录相关的都要先登录（见 auth.py）。
- 页面是 ``webui/`` 里的前端构建出来的静态文件，放在本目录的 ``static/``（不入库，发布时构建，
  见 deploy/scripts/release.sh）。没构建时接口照常可用，打开页面会看到一句提示。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import yaml
from aiohttp import web

from jianshanagent.admin import (
    routes_chat,
    routes_config,
    routes_data,
    routes_system,
    routes_traces,
    routes_wikis,
)
from jianshanagent.admin.auth import Auth, load_token, sessions_file
from jianshanagent.admin.core import STATIC_DIR, Admin, AdminSettings, guard, reply
from jianshanagent.core.config import AppConfig, note_plugin_baseline
from jianshanagent.core.logging import get_logger
from jianshanagent.core.trace import brief

if TYPE_CHECKING:
    from jianshanagent.runtime import Runtime

logger = get_logger(__name__)

_NOT_BUILT = """<!doctype html>
<html lang="zh-CN"><meta charset="utf-8"><title>JianShanAgent 管理后台</title>
<body style="font-family: system-ui; max-width: 40rem; margin: 4rem auto; line-height: 1.7">
<h1>管理后台的页面还没有构建</h1>
<p>接口已经在运行，只是没有前端的静态文件。在仓库里执行：</p>
<pre>cd webui &amp;&amp; npm ci &amp;&amp; npm run build</pre>
<p>然后刷新这一页。线上不用手动做：<code>deploy/scripts/release.sh push</code> 会自动构建。</p>
</body></html>
"""


async def _page(request: web.Request) -> web.StreamResponse:
    """页面和它的静态文件。不认识的路径一律给首页：页面里的路由是前端自己管的。"""
    tail = request.match_info.get("tail", "")
    if tail.startswith("api/") or tail == "api":
        return reply({"error": "没有这个接口"}, 404)
    index = STATIC_DIR / "index.html"
    if not index.is_file():
        return web.Response(text=_NOT_BUILT, content_type="text/html")
    if tail:
        target = (STATIC_DIR / tail).resolve()
        if target.is_relative_to(STATIC_DIR) and target.is_file():
            response = web.FileResponse(target)
            if tail.startswith("assets/"):  # 文件名带内容哈希，可以一直缓存
                response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
            return response
    response = web.FileResponse(index)
    response.headers["Cache-Control"] = "no-cache"
    return response


def _note_plugin_baseline(config: AppConfig) -> None:
    """旧版本的后台存下的插件清单没记「保存时的默认清单」：启动时补记上（见 core/config.py）。

    补不成（文件坏了、盘写不了）不拦启动：清单照旧版本的办法原样用，只是认不出以后新加的插件。
    """
    try:
        if note_plugin_baseline(config):
            logger.info("admin_plugins_baseline_noted")
    except (OSError, ValueError, yaml.YAMLError) as exc:
        logger.warning("admin_plugins_baseline_not_noted", error=brief(repr(exc)))


def setup_admin(app: web.Application, runtime: Runtime) -> Admin:
    """把管理后台挂到 ``app`` 上。要在 HTTP 服务启动之前调用。"""
    settings = AdminSettings.from_config(runtime.config)
    _note_plugin_baseline(runtime.config)
    token, source = load_token(runtime.config, settings.token_file)
    if source == "generated":
        # 令牌本身不进日志，只说它在哪
        logger.warning("admin_token_generated", file=settings.token_file)
    auth = Auth(
        token,
        session_seconds=settings.session_days * 86400,
        max_failures=settings.max_failures,
        lock_seconds=settings.lock_seconds,
        # 退出登录的时间点存在令牌文件旁边：重启后，退出之前发的 Cookie 还是不算
        state_file=sessions_file(settings.token_file),
    )
    admin = Admin(runtime, settings, auth, source)
    app.middlewares.append(guard(admin))
    for module in (
        routes_system,
        routes_traces,
        routes_config,
        routes_chat,
        routes_data,
        routes_wikis,
    ):
        app.add_routes(module.routes(admin))
    app.router.add_get("/{tail:.*}", _page)
    logger.info("admin_enabled", token_source=source, static=(STATIC_DIR / "index.html").is_file())
    return admin


__all__ = ["Admin", "setup_admin"]
