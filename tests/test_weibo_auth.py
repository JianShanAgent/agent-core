"""微博扫码组件离线测试：禁止外网、真实浏览器及执行用户附件。"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import socket
import stat
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import aiohttp
import pytest

from jianshanagent import weibo_browser as browser
from jianshanagent.weibo_auth import (
    WeiboCredentialStore,
    WeiboQrLogin,
    weibo_browser_status,
)

_PNG = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\nfake-qr").decode()
_SECRET = "private-SUB-do-not-leak"
_DISCOVER = browser.discover_browser


@pytest.fixture(autouse=True)
def offline_only(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("测试禁止真实网络或启动真实浏览器")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(aiohttp.ClientSession, "_request", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    monkeypatch.setattr(browser, "discover_browser", lambda: None)


def _cookie(**extra: Any) -> dict[str, Any]:
    return {
        "name": "SUB",
        "value": _SECRET,
        "domain": ".weibo.com",
        "path": "/",
        "expires": time.time() + 3600,
        "secure": True,
        "httpOnly": True,
        **extra,
    }


class FakeBrowser:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.closed = False
        self.qr = _PNG
        self.failure: Exception | None = None
        self.gate: asyncio.Event | None = None
        self.entered = asyncio.Event()

    async def generate_qr(self) -> str:
        self.entered.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.failure:
            raise self.failure
        return self.qr

    async def cookies(self) -> list[dict[str, Any]]:
        if self.failure:
            raise self.failure
        return self.rows

    async def aclose(self) -> None:
        self.closed = True


def test_store_private_atomic_and_status_redaction(tmp_path: Path) -> None:
    store = WeiboCredentialStore(tmp_path / "config")
    assert store.path == tmp_path / "data/weibo/credential.json"
    assert store.status() == {
        "configured": False,
        "locally_usable": False,
        "valid": None,
        "updated_at": None,
    }
    store.save([_cookie(), _cookie(name="XSRF-TOKEN", value="another-secret")])
    assert stat.S_IMODE(store.path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert store.status()["configured"] is True
    assert store.status()["locally_usable"] is True
    assert store.status()["valid"] is None
    assert _SECRET not in json.dumps(store.status())
    assert "cookies" not in store.status()
    assert list(store.path.parent.iterdir()) == [store.path]
    assert not (tmp_path / "config").exists()
    store.clear()
    store.clear()
    assert store.status()["configured"] is False


@pytest.mark.parametrize(
    "extra",
    [
        {"domain": "weibo.com.evil.invalid"},
        {"domain": "sub.weibo.com"},
        {"domain": "..weibo.com"},
        {"domain": "open.weixin.qq.com"},
        {"expires": time.time() - 10},
        {"expires": float("nan")},
        {"expires": float("inf")},
        {"expires": "future"},
        {"expires": True},
        {"expires": 10**1000},
        {"expires": -(10**1000)},
        {"partitionKey": {"topLevelSite": "https://weibo.com"}},
        {"partitionKey": {}},
        {"partitionKeyOpaque": True},
        {"partitioned": True},
        {"name": "SUB\nsecret"},
        {"value": "injected\r\nCookie: secret"},
        {"value": "bad;cookie"},
        {"value": ""},
        {"path": "https://evil.invalid"},
    ],
)
def test_bad_cookie_cannot_create_session(tmp_path: Path, extra: dict[str, Any]) -> None:
    store = WeiboCredentialStore(tmp_path / "config")
    with pytest.raises(ValueError, match="未收到可用"):
        store.save([_cookie(**extra)])
    assert not store.path.exists()


def test_cookie_filter_scope_expiry_partition_and_session_cookie(tmp_path: Path) -> None:
    store = WeiboCredentialStore(tmp_path / "config")
    store.save(
        [
            _cookie(expires=-1),
            _cookie(name="foreign", domain="example.invalid"),
            _cookie(name="expired", expires=time.time() - 5),
            _cookie(name="partitioned", partitionKey="https://weibo.com"),
            _cookie(name="allowed", domain="weibo.com"),
        ]
    )
    payload = json.loads(store.path.read_text())
    assert {row["name"] for row in payload["cookies"]} == {"SUB", "allowed"}
    assert store.status()["locally_usable"] is True
    payload["cookies"][0]["expires"] = time.time() - 1
    store.path.write_text(json.dumps(payload))
    assert store.status()["locally_usable"] is False
    assert store.status()["valid"] is None


def test_dpapi_and_unknown_formats_are_not_imported(tmp_path: Path) -> None:
    store = WeiboCredentialStore(tmp_path / "config")
    store.path.parent.mkdir(parents=True)
    for data in (b"\x01\x00\xffDPAPIencrypted", b"{}", b"[]", b"broken json"):
        store.path.write_bytes(data)
        assert store.status()["configured"] is True
        assert store.status()["locally_usable"] is False
        assert store.status()["valid"] is None


def test_store_rejects_symlinks_and_preserves_old_file_on_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = WeiboCredentialStore(tmp_path / "config")
    store.save([_cookie()])
    previous = store.path.read_bytes()

    def fail(*args: Any, **kwargs: Any) -> None:
        raise OSError("模拟原子替换失败")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError):
        store.save([_cookie(value="new-private-cookie")])
    assert store.path.read_bytes() == previous
    assert list(store.path.parent.iterdir()) == [store.path]
    store.path.unlink()
    outside = tmp_path / "outside"
    outside.write_text("not a credential")
    store.path.symlink_to(outside)
    with pytest.raises(ValueError, match="不安全"):
        store.save([_cookie()])
    assert store.status()["locally_usable"] is False
    store.clear()
    assert outside.read_text() == "not a credential"


def test_dependency_status_and_missing_browser_never_start_process(tmp_path: Path) -> None:
    assert weibo_browser_status()["available"] is False
    assert "Chromium" in weibo_browser_status()["message"]
    store = WeiboCredentialStore(tmp_path / "config")
    assert store.status()["valid"] is None
    login = WeiboQrLogin(store)
    assert login.available is False
    assert login.state == "new"


async def test_missing_browser_is_explicit_error(tmp_path: Path) -> None:
    login = WeiboQrLogin(WeiboCredentialStore(tmp_path / "config"))
    with pytest.raises(ValueError, match="缺少本机"):
        await login.generate()
    assert login.state == "error"
    assert not login.store.status()["configured"]
    await login.aclose()


async def test_qr_wait_confirm_persist_and_cleanup(tmp_path: Path) -> None:
    fake = FakeBrowser()
    login = WeiboQrLogin(WeiboCredentialStore(tmp_path / "config"), lambda: fake)
    assert await login.generate() == _PNG
    assert await login.poll() == "waiting"
    assert login.state != "scanned"  # 无可靠扫描信号，不猜测已扫描。
    fake.rows = [_cookie()]
    assert await login.poll() == "confirmed"
    assert login.store.status()["locally_usable"] is True
    assert login.store.status()["valid"] is None
    assert fake.closed
    assert login._watcher is None
    assert _SECRET not in login.error
    await login.aclose()
    assert login.state == "confirmed"
    with pytest.raises(ValueError, match="新建扫码任务"):
        await login.generate()


async def test_watcher_expires_without_client_polling(tmp_path: Path) -> None:
    fake = FakeBrowser()
    login = WeiboQrLogin(
        WeiboCredentialStore(tmp_path / "config"),
        lambda: fake,
        timeout=0.01,
        poll_interval=0.001,
    )
    await login.generate()
    assert login._watcher is not None
    await asyncio.wait_for(login._watcher, timeout=1)
    assert login.state == "expired"
    assert fake.closed
    assert not login.store.status()["configured"]
    await login.aclose()
    assert login._watcher is None


async def test_cancel_and_clear_do_not_leave_session_or_watcher(tmp_path: Path) -> None:
    fake = FakeBrowser()
    login = WeiboQrLogin(WeiboCredentialStore(tmp_path / "config"), lambda: fake)
    await login.generate()
    await login.aclose()
    login.store.clear()
    fake.rows = [_cookie()]
    assert await login.poll() == "cancelled"
    assert fake.closed
    assert login._watcher is None
    assert not login.store.status()["configured"]
    await login.aclose()


async def test_generation_timeout_and_cancellation_cleanup(tmp_path: Path) -> None:
    fake = FakeBrowser()
    fake.gate = asyncio.Event()
    login = WeiboQrLogin(
        WeiboCredentialStore(tmp_path / "config"),
        lambda: fake,
        operation_timeout=0.01,
    )
    with pytest.raises(ValueError, match="未就绪"):
        await login.generate()
    assert login.state == "error"
    assert fake.closed
    fake = FakeBrowser()
    fake.gate = asyncio.Event()
    login = WeiboQrLogin(WeiboCredentialStore(tmp_path / "config"), lambda: fake)
    task = asyncio.create_task(login.generate())
    await fake.entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert login.state == "cancelled"
    assert fake.closed


async def test_close_during_generation_cannot_resurrect_watcher(tmp_path: Path) -> None:
    fake = FakeBrowser()
    fake.gate = asyncio.Event()
    login = WeiboQrLogin(WeiboCredentialStore(tmp_path / "config"), lambda: fake)
    generating = asyncio.create_task(login.generate())
    await fake.entered.wait()
    closing = asyncio.create_task(login.aclose())
    await asyncio.sleep(0)
    fake.gate.set()
    with pytest.raises(asyncio.CancelledError):
        await generating
    await closing
    assert fake.closed
    assert login.state == "cancelled"
    assert login._watcher is None


async def test_invalid_cookie_does_not_confirm_and_errors_are_redacted(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    fake = FakeBrowser()
    fake.rows = [_cookie(domain="evil.invalid")]
    login = WeiboQrLogin(WeiboCredentialStore(tmp_path / "config"), lambda: fake)
    await login.generate()
    assert await login.poll() == "waiting"
    fake.failure = ValueError(_SECRET + " ws://127.0.0.1:1234/devtools/page/secret")
    assert await login.poll() == "error"
    assert _SECRET not in login.error
    assert "ws://" not in login.error
    assert _SECRET not in caplog.text
    assert fake.closed
    assert not login.store.status()["configured"]


@pytest.mark.parametrize("qr", ["https://evil.invalid/qrcode", "data:image/png;base64,eA=="])
async def test_invalid_qr_never_becomes_waiting(tmp_path: Path, qr: str) -> None:
    fake = FakeBrowser()
    fake.qr = qr
    login = WeiboQrLogin(WeiboCredentialStore(tmp_path / "config"), lambda: fake)
    with pytest.raises(ValueError, match="未就绪"):
        await login.generate()
    assert login.state == "error"
    assert fake.closed


def test_browser_discovery_uses_only_operator_absolute_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = tmp_path / "trusted-chromium"
    candidate.write_text("not executed")
    candidate.chmod(0o700)
    monkeypatch.setenv(browser.BROWSER_ENV, str(candidate))
    assert _DISCOVER() == candidate
    monkeypatch.setenv(browser.BROWSER_ENV, "relative-browser")
    assert _DISCOVER() is None
    monkeypatch.setenv(browser.BROWSER_ENV, str(candidate) + " --unsafe-flag")
    assert _DISCOVER() is None


@pytest.mark.parametrize(
    "url",
    [
        "ws://evil.invalid:1234/devtools/page/id",
        "ws://127.0.0.1:5678/devtools/page/id",
        "ws://127.0.0.1:1234/not-devtools",
        "ws://user@127.0.0.1:1234/devtools/page/id",
        "ws://127.0.0.1:1234/devtools/page/id?token=secret",
        "wss://127.0.0.1:1234/devtools/page/id",
        "ws://localhost:1234/devtools/page/id",
    ],
)
def test_debug_websocket_rejects_remote_or_wrong_process_port(url: str) -> None:
    with pytest.raises(ValueError, match="调试通道异常"):
        browser._debug_websocket(url, 1234)
    assert browser._debug_websocket("ws://127.0.0.1:1234/devtools/page/ABC-123", 1234)


@pytest.mark.parametrize(
    "url, allowed",
    [
        ("about:blank", True),
        ("https://passport.weibo.com/sso/signin", True),
        ("https://open.weixin.qq.com/connect/qrconnect?appid=official", True),
        ("https://weibo.com/", True),
        ("https://weibo.com/u/1234567", True),
        ("https://passport.weibo.com/other", False),
        ("https://open.weixin.qq.com/evil", False),
        ("https://weibo.com.evil.invalid/", False),
        ("http://passport.weibo.com/sso/signin", False),
        ("https://user:password@weibo.com/", False),
        ("https://weibo.com:invalid/", False),
    ],
)
def test_only_official_origin_and_paths_allowed(url: str, allowed: bool) -> None:
    assert browser._trusted_page_url(url) is allowed


class FakeProcess:
    def __init__(self) -> None:
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        return self.returncode or 0


async def test_browser_cdp_flow_uses_temporary_profile_fixed_origin_and_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = FakeProcess()
    command: tuple[str, ...] = ()
    calls: list[tuple[str, dict[str, Any]]] = []

    async def launch(*args: str, **kwargs: Any) -> FakeProcess:
        nonlocal command
        command = args
        return process

    async def connect(self: Any) -> None:
        return None

    async def call(self: Any, method: str, **params: Any) -> dict[str, Any]:
        calls.append((method, params))
        if method == "Page.captureScreenshot":
            return {"data": _PNG.split(",", 1)[1]}
        if method == "Network.getCookies":
            return {"cookies": [_cookie()]}
        return {}

    async def value(self: Any, expression: str) -> Any:
        if expression == browser._CLICK_WECHAT:
            return {"x": 20, "y": 30}
        if expression == browser._QR_CLIP:
            return {"x": 10, "y": 10, "width": 250, "height": 250, "scale": 1}
        return {"origin": "https://open.weixin.qq.com", "path": "/connect/qrconnect"}

    monkeypatch.setattr(browser, "discover_browser", lambda: tmp_path / "trusted-chromium")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    monkeypatch.setattr(browser.ChromiumWeiboBrowser, "_connect", connect)
    monkeypatch.setattr(browser.ChromiumWeiboBrowser, "_call", call)
    monkeypatch.setattr(browser.ChromiumWeiboBrowser, "_value", value)
    instance = browser.ChromiumWeiboBrowser()
    assert await instance.generate_qr() == _PNG
    profile = Path(next(arg.split("=", 1)[1] for arg in command if arg.startswith("--user-data")))
    assert profile.exists()
    assert stat.S_IMODE(profile.stat().st_mode) == 0o700
    assert "--remote-debugging-address=127.0.0.1" in command
    assert "--remote-debugging-port=0" in command
    assert not any("no-sandbox" in arg or "disable-setuid-sandbox" in arg for arg in command)
    assert command[-1] == "about:blank"
    assert ("Page.navigate", {"url": browser._SIGNIN_URL}) in calls
    cookies = await instance.cookies()
    assert cookies[0]["name"] == "SUB"
    assert cookies[0]["value"] == _SECRET
    await instance.aclose()
    assert process.terminated
    assert not profile.exists()
    assert not list(tmp_path.iterdir())


async def test_browser_rejects_cookies_after_leaving_official_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def value(self: Any, expression: str) -> Any:
        return {"origin": "https://evil.invalid", "path": "/"}

    monkeypatch.setattr(browser.ChromiumWeiboBrowser, "_value", value)
    with pytest.raises(ValueError, match="离开允许"):
        await browser.ChromiumWeiboBrowser().cookies()


@pytest.mark.parametrize(
    "ws, accepted",
    [
        ("ws://127.0.0.1:43210/devtools/page/OWN-PAGE", True),
        ("ws://evil.invalid:43210/devtools/page/OWN-PAGE", False),
        ("ws://127.0.0.1:43211/devtools/page/OTHER-PROCESS", False),
    ],
)
async def test_cdp_connection_uses_own_port_no_proxy_and_rejects_redirects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ws: str, accepted: bool
) -> None:
    requests: list[tuple[str, dict[str, Any]]] = []
    websocket_calls: list[str] = []
    setup: dict[str, Any] = {}
    body = json.dumps([{"type": "page", "url": "about:blank", "webSocketDebuggerUrl": ws}]).encode()

    class Content:
        async def read(self, limit: int) -> bytes:
            return body[:limit]

    class Response:
        status = 200
        content_length = len(body)
        content = Content()

        async def __aenter__(self) -> Response:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

    class Session:
        def __init__(self, **kwargs: Any):
            setup.update(kwargs)

        def get(self, url: str, **kwargs: Any) -> Response:
            requests.append((url, kwargs))
            return Response()

        async def ws_connect(self, url: str, **kwargs: Any) -> Any:
            websocket_calls.append(url)
            return self

        async def close(self) -> None:
            return None

    monkeypatch.setattr(aiohttp, "ClientSession", Session)
    instance = browser.ChromiumWeiboBrowser()
    instance._profile = tempfile.TemporaryDirectory(dir=tmp_path)
    profile = Path(instance._profile.name)
    (profile / "DevToolsActivePort").write_text("43210\n/devtools/browser/OWN\n")
    instance._proc = FakeProcess()  # type: ignore[assignment]
    if accepted:
        await instance._connect()
        assert websocket_calls == [ws]
    else:
        with pytest.raises(ValueError, match="调试通道异常"):
            await instance._connect()
        assert not websocket_calls
    assert requests == [("http://127.0.0.1:43210/json/list", {"allow_redirects": False})]
    assert setup["trust_env"] is False
    trace = setup["trace_configs"][0]
    with pytest.raises(ValueError, match="不允许重定向"):
        await trace.on_request_redirect[0](None, None, None)
    await instance.aclose()
    assert not profile.exists()


async def test_cdp_transport_correlates_messages_and_redacts_protocol_errors() -> None:
    sent: list[dict[str, Any]] = []
    messages = [
        {"method": "Page.event", "params": {}},
        {"id": 1, "result": {"cookies": []}},
        {"id": 2, "error": {"message": _SECRET}},
    ]

    class Pipe:
        async def send_json(self, payload: dict[str, Any]) -> None:
            sent.append(payload)

        async def receive(self) -> Any:
            return SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=json.dumps(messages.pop(0)))

        async def close(self) -> None:
            return None

    instance = browser.ChromiumWeiboBrowser()
    instance._socket = Pipe()  # type: ignore[assignment]
    result = await instance._call("Network.getCookies", urls=["https://weibo.com/"])
    assert result == {"cookies": []}
    assert sent[0] == {
        "id": 1,
        "method": "Network.getCookies",
        "params": {"urls": ["https://weibo.com/"]},
    }
    with pytest.raises(ValueError) as caught:
        await instance._call("Runtime.evaluate", expression=browser._CONTEXT)
    assert _SECRET not in str(caught.value)
    await instance.aclose()


async def test_cancelled_browser_close_still_cleans_process_and_profile(tmp_path: Path) -> None:
    gate, entered = asyncio.Event(), asyncio.Event()

    class Pipe:
        async def close(self) -> None:
            entered.set()
            await gate.wait()

    instance = browser.ChromiumWeiboBrowser()
    instance._profile = tempfile.TemporaryDirectory(dir=tmp_path)
    profile = Path(instance._profile.name)
    process = FakeProcess()
    instance._proc = process  # type: ignore[assignment]
    instance._socket = Pipe()  # type: ignore[assignment]
    closing = asyncio.create_task(instance.aclose())
    await entered.wait()
    closing.cancel()
    gate.set()
    with pytest.raises(asyncio.CancelledError):
        await closing
    assert process.terminated
    assert not profile.exists()


async def test_save_failure_does_not_overwrite_old_session_or_leak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = WeiboCredentialStore(tmp_path / "config")
    store.save([_cookie(value="previous-local-session")])
    previous = store.path.read_bytes()
    fake = FakeBrowser()
    fake.rows = [_cookie()]

    def fail(*args: Any, **kwargs: Any) -> None:
        raise OSError(_SECRET + " ws://127.0.0.1:43210/devtools/page/secret")

    monkeypatch.setattr(store, "save", fail)
    login = WeiboQrLogin(store, lambda: fake)
    await login.generate()
    assert await login.poll() == "error"
    assert _SECRET not in login.error
    assert "ws://" not in login.error
    assert store.path.read_bytes() == previous
    assert fake.closed
    await login.aclose()

    def test_operator_credential_file_keeps_session_outside_the_release_directory(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        persistent = tmp_path / "persistent" / "weibo" / "credential.json"
        monkeypatch.setenv("JIANSHANAGENT_WEIBO_CREDENTIAL_FILE", str(persistent))
        store = WeiboCredentialStore(tmp_path / "release" / "config")
        assert store.path == persistent
        store.save([_cookie()])
        assert persistent.is_file()
        assert persistent.stat().st_mode & 0o777 == 0o600
        assert store.status()["locally_usable"]
        assert store.status()["valid"] is None
        assert not (tmp_path / "release" / "data").exists()
