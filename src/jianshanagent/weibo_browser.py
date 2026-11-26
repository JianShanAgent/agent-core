"""受信任本机 Chromium 的临时微博扫码窗口；只通过本进程 loopback CDP 交互。

不连接管理员输入的远程 CDP 或地址，不关闭 Chromium sandbox，不复用用户浏览器资料。
这个模块的构造、依赖发现和状态检查均不会启动浏览器或联网。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import aiohttp

BROWSER_ENV = "JIANSHANAGENT_WEIBO_BROWSER_EXECUTABLE"
_SIGNIN_URL = "https://passport.weibo.com/sso/signin?url=https%3A%2F%2Fweibo.com%2F"
_MAX_MESSAGE = 4 * 1024 * 1024
_CLICK_WECHAT = """(() => {
  if (location.origin !== 'https://passport.weibo.com' ||
      location.pathname !== '/sso/signin') return null;
  const el = [...document.querySelectorAll('span,button,a')].find(x =>
    x.textContent.trim() === '微信登录' && x.getClientRects().length);
  if (!el) return null;
  const r = el.getBoundingClientRect(), x = r.x + r.width / 2, y = r.y + r.height / 2;
  return el.contains(document.elementFromPoint(x,y)) ? {x,y} : null;
})()"""
_QR_CLIP = """(() => {
  if (location.origin !== 'https://open.weixin.qq.com' ||
      location.pathname !== '/connect/qrconnect') return null;
  const el = document.querySelector('img.js_qrcode_img');
  if (!el || !el.getClientRects().length || el.naturalWidth < 100) return null;
  const src = new URL(el.src, location.href);
  if (src.origin !== 'https://open.weixin.qq.com' ||
      !src.pathname.startsWith('/connect/qrcode/')) return null;
  const r = el.getBoundingClientRect();
  return {x: Math.max(0, r.x + scrollX - 12), y: Math.max(0, r.y + scrollY - 12),
          width: r.width + 24, height: r.height + 24, scale: 1};
})()"""
_CONTEXT = """(() => ({origin: location.origin, path: location.pathname}))()"""


def discover_browser() -> Path | None:
    """只检查服务端固定候选或运维环境变量；不执行探测命令、不接受请求参数。"""
    configured = os.environ.get(BROWSER_ENV, "").strip()
    if configured:
        candidates = [Path(configured)]
    else:
        candidates = [
            Path("/usr/bin/chromium"),
            Path("/usr/bin/chromium-browser"),
            Path("/usr/bin/google-chrome"),
            Path("/usr/bin/google-chrome-stable"),
            Path("/opt/google/chrome/chrome"),
            Path("/usr/bin/microsoft-edge"),
            Path("/opt/microsoft/msedge/msedge"),
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
            Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
        ]
        if os.name == "nt":
            for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
                if base:
                    candidates.extend(
                        [
                            Path(base) / "Google/Chrome/Application/chrome.exe",
                            Path(base) / "Microsoft/Edge/Application/msedge.exe",
                        ]
                    )
    for candidate in candidates:
        if candidate.is_absolute() and candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def _official_context(origin: Any, path: Any, *, blank: bool = False) -> bool:
    if not isinstance(origin, str) or not isinstance(path, str):
        return False
    if blank and origin == "about:" and path == "blank":
        return True
    if origin == "https://passport.weibo.com":
        return path == "/sso/signin"
    if origin == "https://open.weixin.qq.com":
        return path == "/connect/qrconnect"
    if origin == "https://weibo.com":
        return path in {"/", "/newlogin"} or re.fullmatch(r"/u/[0-9]+", path) is not None
    return False


def _trusted_page_url(value: Any) -> bool:
    if value == "about:blank":
        return True
    if not isinstance(value, str):
        return False
    try:
        parts = urlsplit(value)
        if parts.username or parts.password or parts.port not in (None, 443):
            return False
    except ValueError:
        return False
    return _official_context(f"{parts.scheme}://{parts.netloc}", parts.path)


def _debug_websocket(value: Any, port: int) -> str:
    """只认可本进程 DevToolsActivePort 指明的 IPv4 loopback 端口。"""
    if not isinstance(value, str):
        raise ValueError("浏览器调试通道异常")
    try:
        parts = urlsplit(value)
        valid = (
            parts.scheme == "ws"
            and parts.hostname == "127.0.0.1"
            and parts.port == port
            and parts.username is None
            and parts.password is None
            and not parts.query
            and not parts.fragment
            and re.fullmatch(r"/devtools/page/[A-Za-z0-9_-]+", parts.path)
        )
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("浏览器调试通道异常")
    return value


class ChromiumWeiboBrowser:
    def __init__(self) -> None:
        self._profile: tempfile.TemporaryDirectory[str] | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._session: aiohttp.ClientSession | None = None
        self._socket: aiohttp.ClientWebSocketResponse | None = None
        self._seq = 0
        self._port = 0
        self._call_lock = asyncio.Lock()

    async def _call(self, method: str, **params: Any) -> dict[str, Any]:
        if self._socket is None:
            raise ValueError("浏览器调试通道未连接")
        async with self._call_lock:
            self._seq += 1
            sequence = self._seq
            await self._socket.send_json({"id": sequence, "method": method, "params": params})
            async with asyncio.timeout(10):
                while True:
                    event = await self._socket.receive()
                    if event.type != aiohttp.WSMsgType.TEXT:
                        raise ValueError("浏览器调试通道已关闭")
                    payload = json.loads(event.data)
                    if payload.get("id") != sequence:
                        continue
                    if "error" in payload or not isinstance(payload.get("result"), dict):
                        raise ValueError("浏览器没有完成官方登录页面操作")
                    return payload["result"]

    async def _value(self, expression: str) -> Any:
        result = await self._call("Runtime.evaluate", expression=expression, returnByValue=True)
        if result.get("exceptionDetails"):
            raise ValueError("官方登录页面不可用")
        return result.get("result", {}).get("value")

    async def _wait_value(self, expression: str) -> dict[str, Any]:
        async with asyncio.timeout(16):
            while True:
                result = await self._value(expression)
                if isinstance(result, dict):
                    return result
                await asyncio.sleep(0.25)

    async def _connect(self) -> None:
        if self._profile is None or self._proc is None:
            raise ValueError("扫码浏览器未启动")
        active = Path(self._profile.name) / "DevToolsActivePort"
        async with asyncio.timeout(12):
            while not active.is_file():
                if self._proc.returncode is not None:
                    raise ValueError("本机浏览器无法启动，请检查安装与 sandbox 运行条件")
                await asyncio.sleep(0.1)
        try:
            lines = active.read_text(encoding="utf-8").splitlines()
            port = int(lines[0])
            if not 1 <= port <= 65535:
                raise ValueError
        except (OSError, ValueError, IndexError):
            raise ValueError("浏览器本机调试端口异常") from None
        self._port = port
        # 不受 HTTP_PROXY / 环境代理影响；禁止自动跟随重定向离开本机端点。
        trace = aiohttp.TraceConfig()

        async def reject_redirect(*args: Any, **kwargs: Any) -> None:
            raise ValueError("浏览器本机调试端点不允许重定向")

        trace.on_request_redirect.append(reject_redirect)
        self._session = aiohttp.ClientSession(
            trust_env=False, timeout=aiohttp.ClientTimeout(total=10), trace_configs=[trace]
        )
        endpoint = f"http://127.0.0.1:{port}/json/list"
        async with self._session.get(endpoint, allow_redirects=False) as response:
            if (
                response.status != 200
                or response.content_length
                and (response.content_length > _MAX_MESSAGE)
            ):
                raise ValueError("浏览器本机调试端点异常")
            body = await response.content.read(_MAX_MESSAGE + 1)
            if len(body) > _MAX_MESSAGE:
                raise ValueError("浏览器调试响应过大")
            pages = json.loads(body)
        if not isinstance(pages, list):
            raise ValueError("浏览器没有返回官方登录页面")
        page = next(
            (
                entry
                for entry in pages
                if isinstance(entry, dict)
                and entry.get("type") == "page"
                and _trusted_page_url(entry.get("url"))
            ),
            None,
        )
        if page is None:
            raise ValueError("浏览器没有返回官方登录页面")
        websocket = _debug_websocket(page.get("webSocketDebuggerUrl"), port)
        self._socket = await self._session.ws_connect(
            websocket, max_msg_size=_MAX_MESSAGE, heartbeat=20
        )

    async def generate_qr(self) -> str:
        if self._proc is not None:
            raise ValueError("扫码浏览器已启动")
        executable = discover_browser()
        if executable is None:
            raise ValueError("未找到本机 Chromium/Chrome/Edge")
        self._profile = tempfile.TemporaryDirectory(prefix="jianshan-weibo-login-")
        os.chmod(self._profile.name, 0o700)
        try:
            self._proc = await asyncio.create_subprocess_exec(
                str(executable),
                "--remote-debugging-address=127.0.0.1",
                "--remote-debugging-port=0",
                f"--user-data-dir={self._profile.name}",
                "--headless=new",
                "--window-size=1280,900",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-extensions",
                "--disable-background-networking",
                "--disable-sync",
                "about:blank",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await self._connect()
            await self._call("Page.enable")
            navigation = await self._call("Page.navigate", url=_SIGNIN_URL)
            if navigation.get("errorText"):
                raise ValueError("微博官方登录页面无法打开")
            point = await self._wait_value(_CLICK_WECHAT)
            if not all(isinstance(point.get(key), (int, float)) for key in ("x", "y")):
                raise ValueError("官方微信登录入口无效")
            for event in ("mousePressed", "mouseReleased"):
                await self._call(
                    "Input.dispatchMouseEvent",
                    type=event,
                    button="left",
                    clickCount=1,
                    x=point["x"],
                    y=point["y"],
                )
            clip = await self._wait_value(_QR_CLIP)
            if (
                not all(
                    isinstance(clip.get(key), (int, float))
                    for key in ("x", "y", "width", "height", "scale")
                )
                or not 100 <= clip["width"] <= 1200
                or not 100 <= clip["height"] <= 1200
            ):
                raise ValueError("官方二维码区域无效")
            shot = await self._call(
                "Page.captureScreenshot", format="png", clip=clip, captureBeyondViewport=True
            )
            data = shot.get("data")
            if not isinstance(data, str) or len(data) > _MAX_MESSAGE:
                raise ValueError("官方二维码截图无效")
            raw = base64.b64decode(data, validate=True)
            if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
                raise ValueError("官方二维码截图无效")
            return "data:image/png;base64," + data
        except BaseException:
            await self.aclose()
            raise

    async def cookies(self) -> list[dict[str, Any]]:
        context = await self._value(_CONTEXT)
        if not isinstance(context, dict) or not _official_context(
            context.get("origin"), context.get("path")
        ):
            raise ValueError("浏览器已离开允许的官方登录页面")
        result = await self._call("Network.getCookies", urls=["https://weibo.com/"])
        cookies = result.get("cookies")
        if not isinstance(cookies, list):
            raise ValueError("浏览器未返回本机会话")
        return cookies

    async def aclose(self) -> None:
        socket, self._socket = self._socket, None
        session, self._session = self._session, None
        process, self._proc = self._proc, None
        profile, self._profile = self._profile, None

        async def cleanup() -> None:
            try:
                for resource in (socket, session):
                    if resource is not None:
                        with contextlib.suppress(Exception):
                            await asyncio.wait_for(resource.close(), timeout=2)
            finally:
                try:
                    if process is not None and process.returncode is None:
                        with contextlib.suppress(ProcessLookupError):
                            process.terminate()
                        try:
                            await asyncio.wait_for(process.wait(), timeout=3)
                        except TimeoutError:
                            with contextlib.suppress(ProcessLookupError):
                                process.kill()
                            await asyncio.wait_for(process.wait(), timeout=3)
                finally:
                    if profile is not None:
                        profile.cleanup()

        # 请求取消不能留下运行中的浏览器与临时 Cookie/profile。
        task = asyncio.create_task(cleanup(), name="weibo-browser-cleanup")
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise
