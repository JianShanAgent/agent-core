"""微博平台会话与显式管理员扫码登录；不会验证或采集微博资料。

登录账号不是资料作者，不能据此改变任何来源的 role。持久化仅在服务端私密数据目录，
状态接口不返回 Cookie、浏览器调试地址或登录凭据。Windows DPAPI 附件不导入、不解密。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import math
import os
import re
import tempfile
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol, TypeGuard

_COOKIE_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
_PNG_HEADER = b"\x89PNG\r\n\x1a\n"
_TERMINAL = frozenset({"confirmed", "expired", "error", "cancelled"})
_MAX_COOKIE_FILE = 1024 * 1024


def _text(value: Any, *, maximum: int = 8192) -> TypeGuard[str]:
    return (
        isinstance(value, str)
        and 0 < len(value) <= maximum
        and all(32 < ord(char) < 127 and char != ";" for char in value)
    )


def _filtered_cookies(rows: Any) -> list[dict[str, Any]]:
    """只保留 weibo.com 作用域、未过期且未分区的 Cookie；不解释外部加密格式。"""
    if not isinstance(rows, list) or len(rows) > 1000:
        return []
    now = time.time()
    cookies: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        domain = row.get("domain")
        if not isinstance(domain, str) or domain.lower() not in {"weibo.com", ".weibo.com"}:
            continue
        if (
            row.get("partitionKey") is not None
            or row.get("partitionKeyOpaque")
            or row.get("partitioned")
        ):
            continue
        name, value, path = row.get("name"), row.get("value"), row.get("path", "/")
        if not _text(name, maximum=256) or not _COOKIE_NAME.fullmatch(name):
            continue
        if not _text(value) or not _text(path, maximum=2048) or not path.startswith("/"):
            continue
        expires = row.get("expires", -1)
        if isinstance(expires, bool) or not isinstance(expires, (int, float)):
            continue
        if expires != -1 and expires <= now:
            continue
        if expires > 253402300799 or not math.isfinite(expires):
            continue
        cookie = {
            "name": name,
            "value": value,
            "domain": domain.lower(),
            "path": path,
            "expires": expires,
            "secure": bool(row.get("secure", True)),
            "httpOnly": bool(row.get("httpOnly", True)),
        }
        cookies[(name, domain.lower(), path)] = cookie
    return list(cookies.values())


def _has_sub(cookies: list[dict[str, Any]]) -> bool:
    return any(cookie["name"] == "SUB" for cookie in cookies)


class WeiboCredentialStore:
    """所有微博来源共享的平台会话；status/clear 均不联网、不启动浏览器。"""

    def __init__(self, config_root: Path):
        configured = os.environ.get("JIANSHANAGENT_WEIBO_CREDENTIAL_FILE", "").strip()
        self.path = (
            Path(configured) if configured else config_root.parent / "data/weibo/credential.json"
        )
        self._verified_at = 0.0
        self._verified_signature: tuple[int, int, int] | None = None

    def _signature(self) -> tuple[int, int, int] | None:
        try:
            result = self.path.stat()
            return result.st_ino, result.st_mtime_ns, result.st_size
        except OSError:
            return None

    def _read(self) -> dict[str, Any] | None:
        try:
            if self.path.is_symlink() or self.path.parent.is_symlink():
                return None
            if self.path.stat().st_size > _MAX_COOKIE_FILE:
                return None
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError):
            return None
        if not isinstance(data, dict) or data.get("format") != 1:
            return None
        return data

    def status(self) -> dict[str, Any]:
        data = self._read()
        cookies = _filtered_cookies(data.get("cookies")) if data else []
        updated_at = data.get("updated_at") if data else None
        locally_usable = _has_sub(cookies)
        verified = (
            locally_usable
            and self._verified_at > 0
            and time.monotonic() - self._verified_at <= 300
            and self._verified_signature is not None
            and self._verified_signature == self._signature()
        )
        return {
            "configured": self.path.is_file(),
            "locally_usable": locally_usable,
            "valid": True if verified else None,
            "updated_at": updated_at if isinstance(updated_at, int) else None,
        }

    def save(self, cookies: list[dict[str, Any]], *, verified: bool = False) -> None:
        filtered = _filtered_cookies(cookies)
        if not _has_sub(filtered):
            raise ValueError("未收到可用的微博 SUB 会话，未保存登录")
        if self.path.parent.is_symlink() or self.path.is_symlink():
            raise ValueError("微博会话存储位置不安全，未保存登录")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        payload = {"format": 1, "cookies": filtered, "updated_at": int(time.time())}
        fd, temporary = tempfile.mkstemp(prefix=".credential.", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                os.fchmod(output.fileno(), 0o600)
                json.dump(payload, output, ensure_ascii=True, allow_nan=False)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary)
        # 只缓存本次可信传输的远端检查结果；不把有效性写入 Cookie 文件或跨重启沿用。
        self._verified_at = time.monotonic() if verified else 0.0
        self._verified_signature = self._signature() if verified else None

    def clear(self) -> None:
        """只移除本机会话；调用方先关闭活动的扫码组件，不撤销远程账号授权。"""
        if self.path.parent.is_symlink():
            raise ValueError("微博会话存储位置不安全")
        self.path.unlink(missing_ok=True)
        self._verified_at = 0.0
        self._verified_signature = None


class BrowserSession(Protocol):
    async def generate_qr(self) -> str: ...
    async def cookies(self) -> list[dict[str, Any]]: ...
    async def aclose(self) -> None: ...


def weibo_browser_status() -> dict[str, Any]:
    from jianshanagent.weibo_browser import discover_browser

    available = discover_browser() is not None
    return {
        "available": available,
        "message": (
            "本机扫码组件可用；仅生成二维码时启动浏览器，登录后尚未校验采集可用性"
            if available
            else "未找到本机 Chromium/Chrome/Edge；请由运维安装浏览器或设置 "
            "JIANSHANAGENT_WEIBO_BROWSER_EXECUTABLE"
        ),
    }


def weibo_login_status() -> dict[str, Any]:
    """HTTP 组件不依赖本机浏览器；实际官方服务错误由取码请求明确报告。"""
    return {
        "available": True,
        "method": "http_wechat",
        "requires_browser": False,
        "message": "通过官方 HTTP 接口获取微信二维码，无需服务器浏览器；本人扫码后再确认微博会话。",
    }


class WeiboQrLogin:
    def __init__(
        self,
        store: WeiboCredentialStore,
        browser_factory: Callable[[], BrowserSession] | None = None,
        *,
        transport_factory: Callable[[], BrowserSession] | None = None,
        timeout: float = 180.0,
        operation_timeout: float = 45.0,
        poll_interval: float = 1.0,
    ):
        from jianshanagent.weibo_http import WechatHttpSession

        if browser_factory is not None and transport_factory is not None:
            raise ValueError("只能选择一种扫码传输")
        if not all(
            math.isfinite(value) and value > 0
            for value in (timeout, operation_timeout, poll_interval)
        ):
            raise ValueError("扫码超时设置必须为有限正数")
        self.store = store
        self.login_id = uuid.uuid4().hex
        self.state = "new"
        self.error = ""
        self.stage = "weibo_init"
        self.error_code = ""
        self.callback_route = ""
        self.callback_scheme = ""
        self.verified = False
        self.method = "browser" if browser_factory is not None else "http_wechat"
        self.available = True
        self._factory: Callable[[], BrowserSession] = (
            transport_factory or browser_factory or (lambda: WechatHttpSession(lifetime=timeout))
        )
        self._timeout = timeout
        self._operation_timeout = operation_timeout
        self._poll_interval = poll_interval
        self._browser: BrowserSession | None = None
        self._watcher: asyncio.Task[None] | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._started = 0.0
        self._lock = asyncio.Lock()
        self._closing = False

    async def _close_browser(self) -> None:
        browser = self._browser
        if browser is None:
            return
        if self._close_task is None:

            async def close() -> None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(browser.aclose(), timeout=self._operation_timeout)

            self._close_task = asyncio.create_task(close())
        closing = self._close_task
        try:
            # 取消 watcher 不取消传输清理；aclose 会继续等待同一个有界任务。
            await asyncio.shield(closing)
        finally:
            if closing.done():
                self._browser = None
                self._close_task = None

    async def generate(self) -> str:
        from jianshanagent.weibo_http import WechatHttpError

        async with self._lock:
            if self.state != "new":
                raise ValueError("本次扫码已经开始或结束，请新建扫码任务")
            self.state = "generating"
            try:
                self._browser = self._factory()
                qr = await asyncio.wait_for(
                    self._browser.generate_qr(), timeout=self._operation_timeout
                )
                if self._closing:
                    raise asyncio.CancelledError
                prefix = "data:image/png;base64,"
                if not isinstance(qr, str) or not qr.startswith(prefix) or len(qr) > 4_000_000:
                    raise ValueError("二维码格式错误")
                image = base64.b64decode(qr[len(prefix) :], validate=True)
                if not image.startswith(_PNG_HEADER):
                    raise ValueError("二维码格式错误")
                self.state = "waiting"
                self._started = time.monotonic()
                self._watcher = asyncio.create_task(self._watch(), name="weibo-qr-login")
                return qr
            except asyncio.CancelledError:
                self.state = "cancelled"
                await self._close_browser()
                raise
            except Exception as exc:
                self.state = "error"
                self.error = (
                    exc.public_message
                    if isinstance(exc, WechatHttpError)
                    else "微博官方微信扫码页未就绪，请稍后重试"
                )
                await self._close_browser()
                raise ValueError(self.error) from None

    async def _check(self) -> str:
        from jianshanagent.weibo_http import WechatHttpError, WechatHttpSession

        async with self._lock:
            if self.state in _TERMINAL or self.state in {"new", "generating"}:
                return self.state
            if time.monotonic() - self._started >= self._timeout:
                self.state = "expired"
                await self._close_browser()
                return self.state
            try:
                transport = self._browser
                if transport is None:
                    raise ValueError("扫码会话已关闭")
                rows = await asyncio.wait_for(
                    transport.cookies(),
                    timeout=min(
                        self._operation_timeout, self._timeout - (time.monotonic() - self._started)
                    ),
                )
                if self._closing:
                    raise asyncio.CancelledError
                if isinstance(transport, WechatHttpSession):
                    self.stage = getattr(transport, "stage", "")
                    self.error_code = getattr(transport, "error_code", "")
                    self.callback_route = getattr(transport, "callback_route", "")
                    self.callback_scheme = getattr(transport, "callback_scheme", "")
                filtered = _filtered_cookies(rows)
                if _has_sub(filtered):
                    verified = getattr(transport, "verified", False) is True
                    if self.method == "http_wechat" and not verified:
                        raise ValueError("微博身份尚未确认，不保存临时会话")
                    self.stage = "credential_save"
                    if verified:
                        self.store.save(filtered, verified=True)
                    else:
                        # 仅显式注入的旧浏览器传输保留“本地保存但未核验”的兼容语义。
                        self.store.save(filtered)
                    self.verified = verified
                    self.state = "confirmed"
                    await self._close_browser()
                else:
                    state = getattr(transport, "state", "waiting")
                    if state in {"waiting", "scanned", "expired", "error", "cancelled"}:
                        self.state = state
                    if self.state == "error":
                        self.error = (
                            transport.error
                            if isinstance(transport, WechatHttpSession)
                            else "微博微信授权或会话确认未完成，请重试"
                        )
                    if self.state in _TERMINAL:
                        await self._close_browser()
            except TimeoutError:
                self.state = "expired"
                await self._close_browser()
            except Exception as exc:
                self.state = "error"
                self.error_code = (
                    exc.code
                    if isinstance(exc, WechatHttpError)
                    else ("save" if self.stage == "credential_save" else "network")
                )
                self.error = (
                    exc.public_message
                    if isinstance(exc, WechatHttpError)
                    else WechatHttpError("save").public_message
                    if self.stage == "credential_save"
                    else "微博扫码会话确认或保存失败，请重新扫码"
                )
                await self._close_browser()
            return self.state

    async def _watch(self) -> None:
        try:
            while self.state not in _TERMINAL:
                await self._check()
                if self.state not in _TERMINAL:
                    remaining = self._timeout - (time.monotonic() - self._started)
                    await asyncio.sleep(min(self._poll_interval, max(0, remaining)))
        except asyncio.CancelledError:
            return

    async def poll(self) -> str:
        # 微信是长轮询；仅 watcher 推进网络，管理 API 始终读快照，不打断终态清理。
        if self.method == "http_wechat":
            return self.state
        state = await self._check()
        if state in _TERMINAL:
            await self._stop_watcher()
        return state

    async def _stop_watcher(self) -> None:
        watcher, self._watcher = self._watcher, None
        if watcher is not None and watcher is not asyncio.current_task():
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher

    async def aclose(self) -> None:
        self._closing = True
        await self._stop_watcher()
        async with self._lock:
            if self.state not in _TERMINAL:
                self.state = "cancelled"
            await self._close_browser()
        await self._stop_watcher()
