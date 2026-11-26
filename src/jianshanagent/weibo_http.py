"""微信官方二维码的纯 HTTP 会话；不执行网页脚本、不绕过人工验证。

二维码就绪不代表登录成功。只有微信确认、微博 HTTP 回调和官方身份检查
全部通过，才交付微博域 Cookie。真实账号扫码仍须由账号持有人完成。
"""

from __future__ import annotations

import asyncio
import base64
import contextvars
import io
import logging
import re
import time
import warnings
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qs, unquote, urlencode, urljoin, urlsplit, urlunsplit

import httpx
from lxml import etree  # type: ignore[import-untyped]
from PIL import Image

_SIGNIN = "https://passport.weibo.com/sso/signin?" + urlencode(
    {"entry": "miniblog", "source": "miniblog", "url": "https://weibo.com/"}
)
_CONFIG = "https://passport.weibo.com/sso/v2/web/config"
_IDENTITY = "https://weibo.com/ajax/config/get"
_TERMINAL = frozenset({"confirmed", "expired", "error", "cancelled"})
_MAX_BODY = 1024 * 1024
_MAX_IMAGE = 512 * 1024
_MAX_REDIRECTS = 8
_MESSAGES = {
    "init": "微博微信登录初始化失败，请稍后重试",
    "qr": "微信官方二维码加载失败，请重新获取",
    "unsafe": "官方登录地址不符合安全限制，已停止登录",
    "poll": "微信扫码状态无法确认，请重新获取二维码",
    "callback": "微博需要额外人工验证或账号绑定，请在官方页面处理后重试",
    "identity": "微博未明确确认登录身份，请在官方页面检查后重试",
    "network": "官方登录服务暂时不可用，请稍后重试",
    "expired": "微信二维码已过期，请重新获取",
    "closed": "微信扫码会话已关闭，请重新获取",
    "save": "服务端保存微博会话失败，请联系管理员",
}
# httpx INFO logs include the complete URL; httpcore DEBUG may include raw headers.
# Scope redaction to THIS request context, not unrelated concurrent HTTP traffic.
_LOGIN_LOG_SCOPE: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "weibo_login_http_log_scope", default=False
)
_PREVIOUS_LOG_FACTORY = logging.getLogRecordFactory()


def _login_log_factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
    record = _PREVIOUS_LOG_FACTORY(*args, **kwargs)
    if _LOGIN_LOG_SCOPE.get() and (
        record.name == "httpx" or record.name.startswith(("httpx.", "httpcore"))
    ):
        record.msg = "Official login HTTP transport (sensitive fields redacted)"
        record.args = ()
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
    return record


logging.setLogRecordFactory(_login_log_factory)


class WechatHttpError(Exception):
    """Only controlled, credential-free messages may cross the login boundary."""

    def __init__(self, code: str) -> None:
        self.code = code if code in _MESSAGES else "network"
        self.public_message = _MESSAGES[self.code]
        super().__init__(self.public_message)


def _safe_url(value: str, *, base: str = "") -> str:
    if not value or len(value) > 16384 or any(ord(c) <= 32 or ord(c) == 127 for c in value):
        raise WechatHttpError("unsafe")
    if "\\" in value:
        raise WechatHttpError("unsafe")
    result = urljoin(base, value) if base else value
    try:
        parts = urlsplit(result)
        if (
            parts.scheme != "https"
            or parts.username is not None
            or parts.password is not None
            or parts.port not in (None, 443)
        ):
            raise WechatHttpError("unsafe")
        host, path = parts.hostname, parts.path
        if parts.fragment:
            # 微信官方授权跳转使用此固定客户端锚点；它不属于 HTTP 请求或授权参数。
            if (host, path, parts.fragment) != (
                "open.weixin.qq.com",
                "/connect/qrconnect",
                "wechat_redirect",
            ):
                raise WechatHttpError("unsafe")
            result = parts._replace(fragment="").geturl()
    except ValueError:
        raise WechatHttpError("unsafe") from None
    allowed = {
        "passport.weibo.com": {
            "/sso/signin",
            "/sso/v2/web/config",
            "/othersitebind/auth",
            "/wbsso/login",
        },
        "passport.weibo.cn": {"/signin/login"},
        "weibo.com": {"/", "/ajax/config/get"},
        "www.weibo.com": {"/"},
        "login.sina.com.cn": {"/sso/login.php", "/sso/crossdomain.php"},
        "open.weixin.qq.com": {"/connect/qrconnect"},
        "long.open.weixin.qq.com": {"/connect/l/qrconnect"},
    }
    image = host == "open.weixin.qq.com" and re.fullmatch(
        r"/connect/qrcode/[A-Za-z0-9_-]{6,256}", path
    )
    if path not in allowed.get(host or "", set()) and not image:
        raise WechatHttpError("unsafe")
    return result


def _query_one(url: str, key: str) -> str:
    values = parse_qs(urlsplit(url).query, keep_blank_values=True).get(key, [])
    if len(values) != 1 or not values[0] or len(values[0]) > 8192:
        raise WechatHttpError("qr")
    return values[0]


def _poll_result(body: str) -> tuple[int, str]:
    """Parse fixed assignments only; no eval, JavaScript engine or general JS parser."""
    match = re.fullmatch(
        r"\s*window\.wx_errcode\s*=\s*(\d{3})\s*;\s*"
        r"(?:window\.wx_code\s*=\s*(['\"])([A-Za-z0-9_-]{0,512})\2\s*;?\s*)?",
        body,
    )
    if not match:
        raise WechatHttpError("poll")
    return int(match[1]), match[3] or ""


def _png(data: bytes) -> str:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                if image.format not in {"PNG", "JPEG", "GIF", "WEBP"}:
                    raise WechatHttpError("qr")
                if not (64 <= image.width <= 2048 and 64 <= image.height <= 2048):
                    raise WechatHttpError("qr")
                if image.width * image.height > 4_000_000:
                    raise WechatHttpError("qr")
                image.load()
                output = io.BytesIO()
                image.convert("RGB").save(output, format="PNG")
                if output.tell() > _MAX_IMAGE:
                    raise WechatHttpError("qr")
                encoded = base64.b64encode(output.getvalue()).decode("ascii")
                return "data:image/png;base64," + encoded
    except WechatHttpError:
        raise
    except Exception:
        raise WechatHttpError("qr") from None


class WechatHttpSession:
    """One memory-only official OAuth attempt, with a bounded poll per cookies()."""

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        *,
        timeout: float = 20,
        poll_timeout: float = 30,
        lifetime: float = 180,
    ) -> None:
        if not (0 < timeout <= 60 and 0 < poll_timeout <= 40 and 0 < lifetime <= 600):
            raise ValueError("HTTP login timeout outside supported bounds")
        self._client = client if client is not None else httpx.AsyncClient(trust_env=False)
        self._timeout = timeout
        self._poll_timeout = poll_timeout
        self._lifetime = lifetime
        self._deadline = time.monotonic() + lifetime
        self._closed = False
        self._request_task: asyncio.Task[Any] | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._uuid = ""
        self._oauth_state = ""
        self._callback = ""
        self._last = 408
        self._verified_cookies: list[dict[str, Any]] = []
        self._lock = asyncio.Lock()
        self.state = "new"
        self.stage = "weibo_init"
        self.error_code = ""
        self.callback_route = ""
        self.callback_scheme = ""
        self.error = ""
        self.verified = False
        # An injected client's previous identity must not authenticate a new QR attempt.
        self._client.cookies.clear()

    def __repr__(self) -> str:
        return f"<WechatHttpSession state={self.state!r} verified={self.verified!r}>"

    def _fail(self, code: str) -> WechatHttpError:
        error = WechatHttpError(code)
        self.state = "error"
        self.error_code = error.code
        self.error = error.public_message
        self.verified = False
        return error

    def _remaining(self) -> float:
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            self.state = "expired"
            self.error = _MESSAGES["expired"]
            raise WechatHttpError("expired")
        return remaining

    async def _request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        data: Mapping[str, str] | None = None,
        poll: bool = False,
        limit: int = _MAX_BODY,
    ) -> httpx.Response:
        url = _safe_url(url)
        host = urlsplit(url).hostname
        timeout = min(self._poll_timeout if poll else self._timeout, self._remaining())
        if self._closed:
            raise WechatHttpError("closed")
        token = _LOGIN_LOG_SCOPE.set(True)
        self._request_task = asyncio.current_task()
        try:
            # build_request still uses httpx's domain-aware CookieJar. Never let injected
            # default credentials or CSRF headers cross the Weibo/WeChat boundary.
            request = self._client.build_request(
                method,
                url,
                headers={"User-Agent": "Mozilla/5.0", **(headers or {})},
                data=data,
                timeout=httpx.Timeout(timeout),
            )
            for name in tuple(request.headers):
                if name.lower() in {
                    "authorization",
                    "proxy-authorization",
                    "x-csrf-token",
                    "x-xsrf-token",
                }:
                    del request.headers[name]
            if host == "passport.weibo.com" and headers and "X-CSRF-TOKEN" in headers:
                request.headers["X-CSRF-TOKEN"] = headers["X-CSRF-TOKEN"]
            # Cookie headers from injected defaults are not trusted; rebuild from jar.
            request.headers.pop("cookie", None)
            self._client.cookies.set_cookie_header(request)
            async with asyncio.timeout(timeout + 0.25):
                response = await self._client.send(
                    request, auth=None, follow_redirects=False, stream=True
                )
                try:
                    if int(response.headers.get("content-length", "0")) > limit:
                        raise WechatHttpError("qr" if limit == _MAX_IMAGE else "network")
                    chunks = bytearray()
                    async for chunk in response.aiter_bytes():
                        chunks.extend(chunk)
                        if len(chunks) > limit:
                            raise WechatHttpError("qr" if limit == _MAX_IMAGE else "network")
                    response._content = bytes(chunks)
                    return response
                finally:
                    await response.aclose()
        finally:
            self._request_task = None
            _LOGIN_LOG_SCOPE.reset(token)

    async def _follow(self, url: str, *, phase: str) -> httpx.Response:
        for _ in range(_MAX_REDIRECTS + 1):
            response = await self._request("GET", url)
            if response.status_code in {301, 302, 303, 307, 308}:
                if phase == "callback":
                    try:
                        target = urlsplit(
                            urljoin(str(response.url), response.headers.get("location", ""))
                        )
                        self.callback_scheme = (
                            target.scheme if target.scheme in {"https", "http"} else "other"
                        )
                        self.callback_route = {
                            ("passport.weibo.cn", "/signin/login"): "weibo_mobile_login",
                            ("weibo.com", "/sso/logincallback"): "weibo_logincallback",
                            ("weibo.com", "/"): "weibo_root",
                            ("www.weibo.com", "/"): "weibo_root",
                            ("passport.weibo.com", "/wbsso/login"): "weibo_wbsso",
                            ("passport.weibo.com", "/othersitebind/auth"): "weibo_auth",
                            ("login.sina.com.cn", "/sso/login.php"): "sina_login",
                            ("login.sina.com.cn", "/sso/crossdomain.php"): "sina_crossdomain",
                        }.get((target.hostname, target.path), "other")
                    except ValueError:
                        self.callback_route = self.callback_scheme = "other"
                url = _safe_url(response.headers.get("location", ""), base=str(response.url))
                host = urlsplit(url).hostname
                if phase == "qr" and host not in {"passport.weibo.com", "open.weixin.qq.com"}:
                    raise WechatHttpError("unsafe")
                if phase == "callback" and host not in {
                    "passport.weibo.com",
                    "passport.weibo.cn",
                    "weibo.com",
                    "www.weibo.com",
                    "login.sina.com.cn",
                }:
                    raise WechatHttpError("unsafe")
                continue
            if response.status_code != 200:
                raise WechatHttpError(phase)
            return response
        raise WechatHttpError("unsafe")

    async def generate_qr(self) -> str:
        async with self._lock:
            if self._closed or self.state != "new":
                raise WechatHttpError("closed")
            try:
                await self._initialize()
                self.stage = "wechat_qr"
                page = await self._follow(self._callback, phase="qr")
                if (page.url.host, page.url.path) != ("open.weixin.qq.com", "/connect/qrconnect"):
                    raise WechatHttpError("qr")
                self._callback = _safe_url(_query_one(str(page.url), "redirect_uri"))
                if (urlsplit(self._callback).hostname, urlsplit(self._callback).path) != (
                    "passport.weibo.com",
                    "/othersitebind/auth",
                ):
                    raise WechatHttpError("unsafe")
                callback_query = parse_qs(urlsplit(self._callback).query)
                if "code" in callback_query or "state" in callback_query:
                    raise WechatHttpError("unsafe")
                self._oauth_state = _query_one(str(page.url), "state")
                root = etree.HTML(page.content, parser=etree.HTMLParser(no_network=True))
                sources = (
                    root.xpath(
                        '//img[contains(concat(" ", normalize-space(@class), " "),'
                        ' " js_qrcode_img ")]/@src'
                    )
                    if root is not None
                    else []
                )
                if (
                    not sources
                    or len(sources) > 16
                    or any(not isinstance(src, str) for src in sources)
                ):
                    raise WechatHttpError("qr")
                # 官方页会在多个响应式布局中重复同一个码；只合并完全相同的安全图链。
                image_urls = tuple(
                    dict.fromkeys(_safe_url(src, base=str(page.url)) for src in sources)
                )
                if len(image_urls) != 1:
                    raise WechatHttpError("qr")
                image_url = image_urls[0]
                if urlsplit(image_url).hostname != "open.weixin.qq.com" or not re.fullmatch(
                    r"/connect/qrcode/[A-Za-z0-9_-]{6,256}", urlsplit(image_url).path
                ):
                    raise WechatHttpError("unsafe")
                self._uuid = urlsplit(image_url).path.rsplit("/", 1)[1]
                image = await self._request("GET", image_url, limit=_MAX_IMAGE)
                if image.status_code != 200:
                    raise WechatHttpError("qr")
                result = _png(image.content)
                self._remaining()
                self.state = "waiting"
                return result
            except asyncio.CancelledError:
                self.state = "cancelled"
                raise
            except WechatHttpError as exc:
                if self.state != "expired":
                    self._fail(exc.code)
                raise
            except Exception:
                raise self._fail("init") from None

    async def _initialize(self) -> None:
        response = await self._request("GET", _SIGNIN)
        if response.status_code != 200:
            raise WechatHttpError("init")
        csrf = [
            cookie.value
            for cookie in self._client.cookies.jar
            if cookie.name == "X-CSRF-TOKEN"
            and cookie.domain
            in {"passport.weibo.com", ".passport.weibo.com", "weibo.com", ".weibo.com"}
            and not cookie.is_expired()
        ]
        if len(csrf) != 1 or not csrf[0]:
            raise WechatHttpError("init")
        response = await self._request(
            "POST",
            _CONFIG,
            data={"entry": "miniblog", "source": "miniblog"},
            headers={
                "X-CSRF-TOKEN": unquote(csrf[0]),
                "X-Requested-With": "XMLHttpRequest",
                "Origin": "https://passport.weibo.com",
                "Referer": _SIGNIN,
            },
        )
        if response.status_code != 200:
            raise WechatHttpError("init")
        payload = response.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        if (
            payload.get("retcode") != 20000000
            or not isinstance(data, dict)
            or data.get("show_wechat") != 1
            or not isinstance(data.get("wechat_url"), str)
        ):
            raise WechatHttpError("init")
        authorization = _safe_url(data["wechat_url"], base=_CONFIG)
        if (urlsplit(authorization).hostname, urlsplit(authorization).path) != (
            "passport.weibo.com",
            "/othersitebind/auth",
        ):
            raise WechatHttpError("unsafe")
        parts = urlsplit(authorization)
        if "r" in parse_qs(parts.query, keep_blank_values=True):
            raise WechatHttpError("unsafe")
        self._callback = urlunsplit(
            (
                parts.scheme,
                parts.netloc,
                parts.path,
                parts.query + "&" + urlencode({"r": "https://weibo.com/"}),
                "",
            )
        )

    async def cookies(self) -> list[dict[str, Any]]:
        async with self._lock:
            if self._closed:
                return []
            if self.verified and self.state == "confirmed":
                return [dict(row) for row in self._verified_cookies]
            if self.state in _TERMINAL or self.state == "new":
                return []
            try:
                self._remaining()
                self.stage = "wechat_poll"
                url = "https://long.open.weixin.qq.com/connect/l/qrconnect?" + urlencode(
                    {
                        "uuid": self._uuid,
                        "last": self._last,
                        "_": int(time.time() * 1000),
                    }
                )
                try:
                    response = await self._request("GET", url, poll=True, limit=8192)
                except (httpx.ReadTimeout, TimeoutError):
                    self._remaining()
                    return []
                self._remaining()
                if response.status_code != 200:
                    raise WechatHttpError("poll")
                status, code = _poll_result(response.text)
                if status == 408:
                    self._last = status
                    return []
                if status == 404:
                    self._last = status
                    self.state = "scanned"
                    return []
                if status != 405 or not code:
                    # Undocumented codes are not guessed to mean login or expiry.
                    raise WechatHttpError("poll")
                await self._complete(code)
                return [dict(row) for row in self._verified_cookies]
            except asyncio.CancelledError:
                self.state = "cancelled"
                raise
            except WechatHttpError as exc:
                if self.state != "expired":
                    self._fail(exc.code)
                return []
            except Exception:
                self._fail("network")
                return []

    async def _complete(self, code: str) -> None:
        self.stage = "weibo_callback"
        parts = urlsplit(self._callback)
        callback = urlunsplit(
            (
                parts.scheme,
                parts.netloc,
                parts.path,
                parts.query + "&" + urlencode({"code": code, "state": self._oauth_state}),
                "",
            )
        )
        landing = await self._follow(callback, phase="callback")
        # Never interpret an unknown callback script, submit a form, send an SMS,
        # register or bind an account. A normal HTTP redirect must reach Weibo.
        if landing.url.host not in {"weibo.com", "www.weibo.com"} or landing.url.path != "/":
            raise WechatHttpError("callback")
        self.stage = "weibo_identity"
        response = await self._request(
            "GET",
            _IDENTITY,
            headers={"Referer": "https://weibo.com/", "Accept": "application/json"},
        )
        if response.status_code != 200:
            raise WechatHttpError("identity")
        payload = response.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        if (
            not isinstance(payload, dict)
            or type(payload.get("ok")) is not int
            or payload["ok"] != 1
            or not isinstance(data, dict)
            or not (
                data.get("login") is True or type(data.get("login")) is int and data["login"] == 1
            )
        ):
            raise WechatHttpError("identity")
        result = self._export_cookies()
        if not any(row["name"] == "SUB" for row in result):
            raise WechatHttpError("identity")
        self._remaining()
        self._verified_cookies = result
        self.verified = True
        self.state = "confirmed"

    def _export_cookies(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for cookie in self._client.cookies.jar:
            if (
                cookie.domain not in {"weibo.com", ".weibo.com"}
                or cookie.is_expired()
                or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,256}", cookie.name)
                or not cookie.value
                or len(cookie.value) > 8192
                or any(ord(c) <= 32 or ord(c) >= 127 or c == ";" for c in cookie.value)
                or not cookie.path
                or not cookie.path.startswith("/")
                or len(cookie.path) > 2048
                or any(ord(c) <= 32 or ord(c) >= 127 for c in cookie.path)
                or cookie.has_nonstandard_attr("Partitioned")
            ):
                continue
            result.append(
                {
                    "name": cookie.name,
                    "value": cookie.value,
                    "domain": cookie.domain,
                    "path": cookie.path,
                    "expires": cookie.expires or -1,
                    "secure": cookie.secure,
                    "httpOnly": cookie.has_nonstandard_attr("HttpOnly"),
                }
            )
        return result

    async def aclose(self) -> None:
        if self._close_task is None:
            self._closed = True
            if self.state not in _TERMINAL:
                self.state = "cancelled"
            active = self._request_task
            if active is asyncio.current_task():
                active = None
            self._close_task = asyncio.create_task(self._finish_close(active))
        # 调用方被取消时不丢弃清理；后续 aclose 可继续等同一个任务。
        await asyncio.shield(self._close_task)

    async def _finish_close(self, active: asyncio.Task[Any] | None) -> None:
        if active is not None:
            active.cancel()
            try:
                await active
            except (asyncio.CancelledError, WechatHttpError):
                pass
        self._uuid = self._oauth_state = self._callback = ""
        self._verified_cookies.clear()
        self._client.cookies.clear()
        token = _LOGIN_LOG_SCOPE.set(True)
        try:
            await self._client.aclose()
        finally:
            _LOGIN_LOG_SCOPE.reset(token)
