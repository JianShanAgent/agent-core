"""微博平台共享的 HTTP 微信扫码会话；不接受 URL、代码或 Cookie 上传。"""

from __future__ import annotations

import asyncio
from typing import Any

from aiohttp import web

from jianshanagent.admin.core import Admin, fail, read_body, reply
from jianshanagent.core.sources import source_instances
from jianshanagent.weibo_auth import WeiboCredentialStore, WeiboQrLogin, weibo_login_status

_BUSY = {"new", "generating", "waiting", "scanned"}
_PREFIX = "/api/sources/platforms/weibo"
_STATES = _BUSY | {"confirmed", "expired", "error", "cancelled"}


def routes(admin: Admin) -> list[web.RouteDef]:
    changing = asyncio.Lock()

    def store() -> Any:
        if not any(item.platform.id == "weibo" for item in source_instances(admin.disk_config())):
            fail(409, "请先登记微博资料来源；会话由这些来源共享，不代表帖子作者身份")
        if admin.weibo is None:
            admin.weibo = WeiboCredentialStore(admin.config.root)
        return admin.weibo

    def login_state(login: Any) -> str:
        return login.state if login.state in _STATES else "error"

    def login_method(login: Any) -> str:
        # Only local implementation labels, never an upstream OAuth state or URL.
        method = getattr(login, "method", "http_wechat")
        return "http_wechat" if method == "http_wechat" else "browser"

    def login_error(login: Any) -> str:
        if login is None or not login.error:
            return ""
        # 只展示本地固定提示，既说明人工验证阶段，也绝不透传上游错误或凭据。
        from jianshanagent.weibo_http import WechatHttpError

        for code in (
            "init",
            "qr",
            "unsafe",
            "poll",
            "callback",
            "identity",
            "network",
            "expired",
            "closed",
            "save",
        ):
            message = WechatHttpError(code).public_message
            if login.error == message:
                return message
        return "登录未完成，请重试"

    def login_diagnostics(login: Any) -> dict[str, str]:
        # 仅保留固定枚举，不保存/展示原始 URL、响应正文、code、state 或 Cookie。
        choices = {
            "stage": {
                "weibo_init",
                "wechat_qr",
                "wechat_poll",
                "weibo_callback",
                "weibo_identity",
                "credential_save",
            },
            "error_code": {
                "init",
                "qr",
                "unsafe",
                "poll",
                "callback",
                "identity",
                "network",
                "expired",
                "closed",
                "save",
            },
            "callback_route": {
                "weibo_logincallback",
                "weibo_root",
                "weibo_wbsso",
                "weibo_auth",
                "sina_login",
                "sina_crossdomain",
                "other",
            },
            "callback_scheme": {"https", "http", "other"},
        }
        return {
            key: value
            if isinstance(value := getattr(login, key, ""), str) and value in allowed
            else ""
            for key, allowed in choices.items()
        }

    def session() -> dict[str, Any]:
        state = store().status()
        local = weibo_login_status()
        login = admin.weibo_qr
        return {
            "configured": bool(state.get("configured")),
            "locally_usable": bool(state.get("locally_usable")),
            "valid": True if state.get("valid") is True else None,
            "updated_at": state.get("updated_at"),
            "available": bool(local.get("available")),
            "message": str(local.get("message") or ""),
            "method": login_method(login) if login is not None else "http_wechat",
            "requires_browser": bool(local.get("requires_browser", False)),
            "login": login_state(login) if login is not None else "",
            "login_id": login.login_id if login is not None else None,
            "verified": bool(login is not None and getattr(login, "verified", False))
            and state.get("valid") is True,
            "error": login_error(login),
            **login_diagnostics(login),
        }

    async def empty_body(request: web.Request) -> None:
        if request.can_read_body:
            body = await read_body(request, limit=4096)
            if body:
                fail(400, "微信扫码不接受地址、路径、代码或凭据参数")

    def matching_login(login_id: Any) -> Any:
        login = admin.weibo_qr
        if (
            not isinstance(login_id, str)
            or not 1 <= len(login_id) <= 128
            or login is None
            or login_id != login.login_id
        ):
            fail(409, "微博登录会话已变化，请刷新页面后重试")
        return login

    async def status(_request: web.Request) -> web.Response:
        return reply(session())

    async def qr(request: web.Request) -> web.Response:
        await empty_body(request)
        async with changing:
            credential = store()
            if admin.weibo_qr is not None and admin.weibo_qr.state in _BUSY:
                fail(409, "已经有一个微博登录正在进行")
            local = weibo_login_status()
            if not local.get("available"):
                fail(409, str(local.get("message") or "微信扫码登录组件不可用"))
            if admin.weibo_qr is not None:
                await admin.weibo_qr.aclose()
            login = WeiboQrLogin(credential)
            admin.weibo_qr = login
            try:
                image = await login.generate()
            except Exception:
                await login.aclose()
                fail(502, "微博微信扫码二维码生成失败，请稍后重试")
            if not isinstance(image, str) or not image.startswith("data:image/"):
                await login.aclose()
                fail(502, "登录组件没有返回可展示的二维码")
            return reply(
                {
                    "image": image,
                    "state": login_state(login),
                    "available": login.available,
                    "login_id": login.login_id,
                    "method": login_method(login),
                }
            )

    async def poll(request: web.Request) -> web.Response:
        store()
        if set(request.query) - {"login_id"} or len(request.query.getall("login_id", [])) > 1:
            fail(400, "轮询只接受本地登录会话标识")
        async with changing:
            login = matching_login(request.query.get("login_id"))
            try:
                await login.poll()
            except Exception:
                await login.aclose()
                fail(502, "微博登录状态读取失败，请重试")
            state = session()
            return reply(
                {
                    "state": login_state(login),
                    "login_id": login.login_id,
                    "method": login_method(login),
                    "verified": state["verified"],
                    "valid": state["valid"],
                    "error": login_error(login),
                    "stage": state["stage"],
                    "error_code": state["error_code"],
                    "callback_route": state["callback_route"],
                    "callback_scheme": state["callback_scheme"],
                }
            )

    async def cancel(request: web.Request) -> web.Response:
        body = await read_body(request, limit=4096) if request.can_read_body else {}
        if set(body) - {"login_id"}:
            fail(400, "取消只接受本地登录会话标识")
        store()
        async with changing:
            login = matching_login(body.get("login_id"))
            await login.aclose()
            admin.weibo_qr = None
        return reply({"state": "cancelled", "login_id": login.login_id})

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
