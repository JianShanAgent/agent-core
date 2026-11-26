"""微博平台共享的本机会话；不接受 URL、CDP 地址或 Cookie 上传。"""

from __future__ import annotations

import asyncio
from typing import Any

from aiohttp import web

from jianshanagent.admin.core import Admin, fail, read_body, reply
from jianshanagent.core.sources import source_instances
from jianshanagent.weibo_auth import WeiboCredentialStore, WeiboQrLogin, weibo_browser_status

_BUSY = {"generating", "waiting", "scanned"}
_PREFIX = "/api/sources/platforms/weibo"


def routes(admin: Admin) -> list[web.RouteDef]:
    changing = asyncio.Lock()

    def store() -> Any:
        if not any(item.platform.id == "weibo" for item in source_instances(admin.disk_config())):
            fail(409, "请先登记微博资料来源；会话由这些来源共享，不代表帖子作者身份")
        if admin.weibo is None:
            admin.weibo = WeiboCredentialStore(admin.config.root)
        return admin.weibo

    def session() -> dict[str, Any]:
        state = store().status()
        local = weibo_browser_status()
        return {
            "configured": bool(state.get("configured")),
            "locally_usable": bool(state.get("locally_usable")),
            # Local Cookie presence never establishes whether the remote session is valid.
            "valid": None,
            "updated_at": state.get("updated_at"),
            "available": bool(local.get("available")),
            "message": str(local.get("message") or ""),
            "login": str(admin.weibo_qr.state) if admin.weibo_qr is not None else "",
            "error": "登录未完成，请重试"
            if admin.weibo_qr is not None and admin.weibo_qr.error
            else "",
        }

    async def empty_body(request: web.Request) -> None:
        if request.can_read_body:
            body = await read_body(request, limit=4096)
            if body:
                fail(400, "登录只使用受信任的本机浏览器，不接受地址、路径、代码或凭据参数")

    async def status(_request: web.Request) -> web.Response:
        return reply(session())

    async def qr(request: web.Request) -> web.Response:
        await empty_body(request)
        async with changing:
            credential = store()
            if admin.weibo_qr is not None and admin.weibo_qr.state in _BUSY:
                fail(409, "已经有一个微博登录正在进行")
            local = weibo_browser_status()
            if not local.get("available"):
                fail(409, str(local.get("message") or "没有可用的本机浏览器"))
            if admin.weibo_qr is not None:
                await admin.weibo_qr.aclose()
            login = WeiboQrLogin(credential)
            admin.weibo_qr = login
            try:
                image = await login.generate()
            except Exception:
                await login.aclose()
                fail(502, "微博登录二维码生成失败，请检查本机浏览器后重试")
            if not isinstance(image, str) or not image.startswith("data:image/"):
                await login.aclose()
                fail(502, "登录组件没有返回可展示的二维码")
            return reply({"image": image, "state": login.state, "available": login.available})

    async def poll(_request: web.Request) -> web.Response:
        store()
        async with changing:
            login = admin.weibo_qr
            if login is None:
                fail(409, "还没有待确认的微博登录")
            try:
                state = await login.poll()
            except Exception:
                await login.aclose()
                fail(502, "微博登录状态读取失败，请重试")
            return reply({"state": state, "error": "登录未完成，请重试" if login.error else ""})

    async def cancel(request: web.Request) -> web.Response:
        await empty_body(request)
        store()
        async with changing:
            if admin.weibo_qr is not None:
                await admin.weibo_qr.aclose()
                admin.weibo_qr = None
        return reply({"state": "cancelled"})

    async def clear(request: web.Request) -> web.Response:
        await empty_body(request)
        credential = store()
        async with changing:
            if admin.weibo_qr is not None:
                await admin.weibo_qr.aclose()
                admin.weibo_qr = None
            credential.clear()
        return reply(session())

    return [
        web.get(_PREFIX + "/session", status),
        web.post(_PREFIX + "/qr", qr),
        web.get(_PREFIX + "/poll", poll),
        web.post(_PREFIX + "/cancel", cancel),
        web.delete(_PREFIX + "/session", clear),
    ]
