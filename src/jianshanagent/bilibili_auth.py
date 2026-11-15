"""B 站 Web 登录凭据：扫码登录、私密持久化和 Cookie 刷新。"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from bilibili_api import Credential
from bilibili_api.login_v2 import QrCodeLogin, QrCodeLoginEvents


class BilibiliCredentialStore:
    def __init__(self, root: Path):
        configured = os.environ.get("JIANSHANAGENT_BILIBILI_CREDENTIAL_FILE", "").strip()
        self.path = (
            Path(configured) if configured else root.parent / "data/bilibili/credential.json"
        )
        self._lock = asyncio.Lock()
        self._last_check = 0.0

    def _read(self) -> dict[str, Any] | None:
        try:
            value = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    def _write(self, credential: Credential) -> None:
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

    def credential(self) -> Credential | None:
        data = self._read()
        if not data or not data.get("sessdata"):
            return None
        return Credential(**{key: data.get(key) for key in (
            "sessdata", "bili_jct", "buvid3", "buvid4", "dedeuserid", "ac_time_value"
        )})

    def cookie(self) -> str:
        credential = self.credential()
        return "" if credential is None else "; ".join(
            f"{key}={value}" for key, value in credential.get_cookies().items() if value
        )

    async def refresh_if_needed(self) -> bool:
        if time.monotonic() - self._last_check < 86400:
            return self.credential() is not None
        async with self._lock:
            if time.monotonic() - self._last_check < 86400:
                return self.credential() is not None
            credential = self.credential()
            self._last_check = time.monotonic()
            if credential is None:
                return False
            try:
                if not await credential.check_valid():
                    return False
                if await credential.check_refresh():
                    await credential.refresh()
                    self._write(credential)
                return True
            except Exception:
                return False


class BilibiliQrLogin:
    def __init__(self, store: BilibiliCredentialStore):
        self.store = store
        self.login = QrCodeLogin()
        self.created_at = time.time()
        self.state = "new"
        self.error = ""

    async def generate(self) -> str:
        await self.login.generate_qrcode()
        self.state = "waiting"
        picture = self.login.get_qrcode_picture()
        data = getattr(picture, "content", None)
        if data is None:
            raise RuntimeError("B 站二维码生成失败")
        import base64
        return "data:image/png;base64," + base64.b64encode(data).decode()

    async def poll(self) -> str:
        if time.time() - self.created_at > 180:
            self.state = "expired"
            return self.state
        event = await self.login.check_state()
        if event is QrCodeLoginEvents.SCAN:
            self.state = "scanned"
        elif event is QrCodeLoginEvents.CONF:
            self.state = "confirmed"
        elif event is QrCodeLoginEvents.TIMEOUT:
            self.state = "expired"
        elif event is QrCodeLoginEvents.DONE:
            credential = self.login.get_credential()
            self.store._write(credential)
            self.state = "done"
        return self.state
