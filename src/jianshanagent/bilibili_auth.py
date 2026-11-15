"""B 站 Web 登录凭据：扫码登录、私密持久化和 Cookie 刷新。

扫码成功时，Cookie 在轮询响应的 ``Set-Cookie`` 里。``bilibili-api`` 17.4.2 只从返回地址里切，
当前接口的地址已经不带 ``SESSDATA``，会把空凭据当成登录成功。这里自己读响应头。
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import httpx
import qrcode
from bilibili_api import Credential

_GENERATE_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
_POLL_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Referer": "https://passport.bilibili.com/",
}
_COOKIE_NAMES = ("SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5", "sid")


def _response_cookies(response: httpx.Response) -> dict[str, str]:
    """从 ``Set-Cookie`` 取出登录凭据。不依赖 Cookie 域名是否和请求域名一致。"""
    found: dict[str, str] = {}
    for header in response.headers.get_list("set-cookie"):
        pair = header.split(";", 1)[0]
        name, separator, value = pair.partition("=")
        if separator and name.strip() in _COOKIE_NAMES:
            found[name.strip()] = unquote(value.strip())
    return found


def _url_cookies(url: str) -> dict[str, str]:
    """旧接口把凭据放在跨域地址的查询参数里，留作兜底。"""
    query = parse_qs(urlsplit(url).query)
    return {
        name: unquote(values[0])
        for name in _COOKIE_NAMES
        if (values := query.get(name)) and values[0]
    }


class BilibiliCredentialStore:
    def __init__(self, root: Path):
        configured = os.environ.get("JIANSHANAGENT_BILIBILI_CREDENTIAL_FILE", "").strip()
        self.path = (
            Path(configured) if configured else root.parent / "data/bilibili/credential.json"
        )
        self._lock = asyncio.Lock()
        self._last_check = 0.0
        self._valid: bool | None = None

    def _read(self) -> dict[str, Any] | None:
        try:
            value = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    def _write(self, credential: Credential) -> None:
        if not credential.sessdata:
            raise ValueError("登录响应里没有 SESSDATA，没有保存")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "sessdata": credential.sessdata,
            "bili_jct": credential.bili_jct,
            "buvid3": credential.buvid3,
            "buvid4": credential.buvid4,
            "dedeuserid": credential.dedeuserid,
            "ac_time_value": credential.ac_time_value,
            "updated_at": int(time.time()),
        }
        fd, name = tempfile.mkstemp(prefix="credential.", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                json.dump(payload, file, ensure_ascii=False)
                file.write("\n")
            os.chmod(name, 0o600)
            os.replace(name, self.path)
        finally:
            try:
                os.unlink(name)
            except FileNotFoundError:
                pass
        self._valid = None
        self._last_check = 0.0

    def credential(self) -> Credential | None:
        data = self._read()
        if not data or not data.get("sessdata"):
            return None
        return Credential(
            **{
                key: data.get(key)
                for key in (
                    "sessdata",
                    "bili_jct",
                    "buvid3",
                    "buvid4",
                    "dedeuserid",
                    "ac_time_value",
                )
            }
        )

    def cookie(self) -> str:
        credential = self.credential()
        if credential is None:
            return ""
        return "; ".join(
            f"{key}={value}" for key, value in credential.get_cookies().items() if value
        )

    async def refresh_if_needed(self) -> bool:
        """凭据还在就返回 True。一天最多向 B 站确认一次，确认失败不把已保存的凭据当成没登录。"""
        if self.credential() is None:
            return False
        if self._valid is not None and time.monotonic() - self._last_check < 86400:
            return self._valid
        async with self._lock:
            if self._valid is not None and time.monotonic() - self._last_check < 86400:
                return self._valid
            credential = self.credential()
            self._last_check = time.monotonic()
            if credential is None:
                self._valid = False
                return False
            try:
                self._valid = bool(await credential.check_valid())
                if self._valid and await credential.check_refresh():
                    await credential.refresh()
                    self._write(credential)
            except Exception:
                self._valid = False
            return self._valid


class BilibiliQrLogin:
    def __init__(
        self,
        store: BilibiliCredentialStore,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.store = store
        self.transport = transport
        self.key = ""
        self.created_at = time.time()
        self.state = "new"
        self.error = ""

    async def _get(self, url: str, params: dict[str, str]) -> httpx.Response:
        async with httpx.AsyncClient(
            transport=self.transport, headers=_HEADERS, timeout=20, follow_redirects=False
        ) as client:
            response = await client.get(url, params=params)
        if response.status_code >= 400:
            raise RuntimeError(f"B 站登录接口返回 HTTP {response.status_code}")
        return response

    async def generate(self) -> str:
        response = await self._get(_GENERATE_URL, {"source": "main-fe-header"})
        data = response.json().get("data") or {}
        url, key = str(data.get("url") or ""), str(data.get("qrcode_key") or "")
        if not url or not key:
            raise RuntimeError("B 站没有返回二维码")
        self.key = key
        self.created_at = time.time()
        self.state = "waiting"
        image = qrcode.make(url)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()

    async def poll(self) -> str:
        if not self.key:
            raise RuntimeError("还没有生成二维码")
        if time.time() - self.created_at > 180:
            self.state = "expired"
            return self.state
        response = await self._get(_POLL_URL, {"qrcode_key": self.key})
        data = (response.json().get("data") or {})
        code = int(data.get("code") or 0)
        if code == 86101:
            self.state = "waiting"
        elif code == 86090:
            self.state = "confirmed"
        elif code == 86038:
            self.state = "expired"
        elif code == 0:
            cookies = _response_cookies(response) | _url_cookies(str(data.get("url") or ""))
            sessdata = cookies.get("SESSDATA", "")
            if not sessdata:
                raise RuntimeError("B 站确认了登录，但响应里没有 SESSDATA")
            self.store._write(
                Credential(
                    sessdata=sessdata,
                    bili_jct=cookies.get("bili_jct", ""),
                    dedeuserid=cookies.get("DedeUserID", ""),
                    ac_time_value=str(data.get("refresh_token") or ""),
                )
            )
            self.state = "done"
        else:
            raise RuntimeError(str(data.get("message") or f"B 站返回了未知登录状态 {code}"))
        return self.state
