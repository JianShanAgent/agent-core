"""微信扫码管理 API 的 ID 隔离和身份验证语义；全部使用本地假会话。"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from uuid import UUID

import pytest

from jianshanagent.admin import routes_sources_session
from jianshanagent.weibo_auth import weibo_login_status
from test_sources_admin import FakeLogin, LocalEnv
from test_sources_admin import fake_login as fake_login
from test_sources_admin import local as local

PREFIX = "/api/sources/platforms/weibo"


async def test_login_ids_isolate_old_tabs(local: LocalEnv, fake_login: None) -> None:
    await local.create()
    first = await local.send("POST", PREFIX + "/qr", {})
    first_login = FakeLogin.instances[-1]
    assert first["login_id"] == first_login.login_id
    assert first["method"] == "http_wechat"
    assert (await local.send("GET", PREFIX + "/session"))["login_id"] == first["login_id"]
    # A legacy client must not poll or cancel a newer user's shared login.
    for suffix in ("", "?login_id=wrong", "?login_id=" + "x" * 129):
        await local.send("GET", PREFIX + "/poll" + suffix, status=409)
    for body in ({}, {"login_id": None}, {"login_id": []}, {"login_id": "wrong"}):
        await local.send("POST", PREFIX + "/cancel", body, 409)
    assert first_login.state == "waiting" and not first_login.closed

    await local.send("POST", PREFIX + "/cancel", {"login_id": first["login_id"]})
    second = await local.send("POST", PREFIX + "/qr", {})
    second_login = FakeLogin.instances[-1]
    assert second["login_id"] != first["login_id"]
    await local.send("GET", PREFIX + "/poll?login_id=" + first["login_id"], status=409)
    await local.send("POST", PREFIX + "/cancel", {"login_id": first["login_id"]}, 409)
    assert second_login.state == "waiting" and not second_login.closed
    assert (await local.send("GET", PREFIX + "/session"))["login_id"] == second["login_id"]
    await local.send("POST", PREFIX + "/cancel", {"login_id": second["login_id"]})
    assert second_login.closed
    assert (await local.send("GET", PREFIX + "/session"))["login_id"] is None


@pytest.mark.parametrize(
    ("valid", "verified", "expected_valid", "expected_verified"),
    [
        (None, False, None, False),
        (None, True, None, False),
        (True, False, True, False),
        (True, True, True, True),
        (False, True, None, False),
        (1, True, None, False),
    ],
)
async def test_status_never_infers_remote_validity(
    local: LocalEnv,
    fake_login: None,
    monkeypatch: pytest.MonkeyPatch,
    valid: Any,
    verified: bool,
    expected_valid: bool | None,
    expected_verified: bool,
) -> None:
    await local.create()
    qr = await local.send("POST", PREFIX + "/qr", {})
    login = FakeLogin.instances[-1]
    login.verified = verified
    monkeypatch.setattr(
        login.store,
        "status",
        lambda: {
            "configured": True,
            "locally_usable": True,
            "valid": valid,
            "updated_at": 123,
            "cookie": "private-cookie-value",
        },
    )
    polled = await local.send("GET", PREFIX + "/poll?login_id=" + qr["login_id"])
    assert polled["state"] == "confirmed"
    assert polled["valid"] is expected_valid
    assert polled["verified"] is expected_verified
    status = await local.send("GET", PREFIX + "/session")
    assert status["valid"] is expected_valid
    assert status["verified"] is expected_verified
    assert "private-cookie" not in json.dumps([status, polled])


@pytest.mark.parametrize("code", ["callback", "identity", "untrusted"])
async def test_only_fixed_manual_verification_errors_are_displayed(
    local: LocalEnv, fake_login: None, code: str
) -> None:
    from jianshanagent.weibo_http import WechatHttpError

    await local.create()
    qr = await local.send("POST", PREFIX + "/qr", {})
    login = FakeLogin.instances[-1]
    if code == "untrusted":
        login.error = "upstream-secret-cookie https://evil.example/?code=private"
        expected = "登录未完成，请重试"
    else:
        expected = login.error = WechatHttpError(code).public_message
    status = await local.send("GET", PREFIX + "/session")
    polled = await local.send("GET", PREFIX + "/poll?login_id=" + qr["login_id"])
    assert status["error"] == polled["error"] == expected
    assert "upstream-secret-cookie" not in json.dumps([status, polled])
    assert "evil.example" not in json.dumps([status, polled])


async def test_auth_csrf_whitelist_and_limits(local: LocalEnv, fake_login: None) -> None:
    await local.create()
    qr = await local.send("POST", PREFIX + "/qr", {})
    login = FakeLogin.instances[-1]
    for method, path, body in (
        ("POST", "/qr", {}),
        ("POST", "/cancel", {"login_id": qr["login_id"]}),
        ("DELETE", "/session", {}),
    ):
        response = await local.client.request(method, PREFIX + path, json=body)
        assert response.status == 403
    for field in ("url", "callback", "cookies", "code", "state", "executable_path", "cdp_url"):
        await local.send("POST", PREFIX + "/qr", {field: "secret"}, 400)
        await local.send(
            "POST", PREFIX + "/cancel", {"login_id": qr["login_id"], field: "secret"}, 400
        )
        await local.send("DELETE", PREFIX + "/session", {field: "secret"}, 400)
    for path in ("/qr", "/cancel"):
        await local.send("POST", PREFIX + path, {"login_id": "x" * 4096}, 413)
    await local.send("DELETE", PREFIX + "/session", {"cookies": "x" * 4096}, 413)
    await local.send("GET", PREFIX + "/poll?login_id=" + qr["login_id"] + "&url=secret", status=400)
    await local.send("GET", PREFIX + "/poll?login_id=a&login_id=b", status=400)
    assert login.state == "waiting" and not login.closed

    local.client.session.cookie_jar.clear()
    for method, suffix in (
        ("GET", "/session"),
        ("GET", "/poll"),
        ("POST", "/qr"),
        ("POST", "/cancel"),
        ("DELETE", "/session"),
    ):
        await local.send(method, PREFIX + suffix, status=401)
    assert not login.closed


async def test_generating_is_busy_and_cancel_waits_for_bounded_generation(
    local: LocalEnv, fake_login: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    await local.create()
    started, release = asyncio.Event(), asyncio.Event()

    async def generate(login: FakeLogin) -> str:
        login.state = "generating"
        started.set()
        await asyncio.wait_for(release.wait(), timeout=3)
        login.state = "waiting"
        return "data:image/png;base64,aGVsbG8="

    monkeypatch.setattr(FakeLogin, "generate", generate)
    qr_task = asyncio.create_task(local.send("POST", PREFIX + "/qr", {}))
    await asyncio.wait_for(started.wait(), timeout=2)
    status = await local.send("GET", PREFIX + "/session")
    assert status["login"] == "generating" and status["login_id"]
    second_task = asyncio.create_task(local.send("POST", PREFIX + "/qr", {}, 409))
    release.set()
    qr, _second = await asyncio.gather(qr_task, second_task)
    assert len(FakeLogin.instances) == 1
    await local.send("POST", PREFIX + "/cancel", {"login_id": qr["login_id"]})
    assert FakeLogin.instances[-1].closed

    # Cancelling while generation is in flight must address that same local ID.
    started.clear()
    release.clear()
    qr_task = asyncio.create_task(local.send("POST", PREFIX + "/qr", {}))
    await asyncio.wait_for(started.wait(), timeout=2)
    login = FakeLogin.instances[-1]
    cancel_task = asyncio.create_task(
        local.send("POST", PREFIX + "/cancel", {"login_id": login.login_id})
    )
    release.set()
    _qr, cancelled = await asyncio.gather(qr_task, cancel_task)
    assert cancelled["login_id"] == login.login_id
    assert login.closed and local.admin.weibo_qr is None

    # Clear also waits for generation and must close before deleting credentials.
    started.clear()
    release.clear()
    qr_task = asyncio.create_task(local.send("POST", PREFIX + "/qr", {}))
    await asyncio.wait_for(started.wait(), timeout=2)
    login = FakeLogin.instances[-1]
    original_clear = login.store.clear

    def clear_after_close() -> None:
        assert login.closed
        original_clear()

    monkeypatch.setattr(login.store, "clear", clear_after_close)
    clear_task = asyncio.create_task(local.send("DELETE", PREFIX + "/session", {}))
    release.set()
    await asyncio.gather(qr_task, clear_task)
    assert login.closed and local.admin.weibo_qr is None


@pytest.mark.parametrize("phase", ["generate", "poll"])
async def test_failures_are_redacted_and_preserve_saved_session(
    local: LocalEnv, fake_login: None, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    await local.create()
    await local.send("GET", PREFIX + "/session")
    local.admin.weibo.configured = True

    async def broken(_login: FakeLogin) -> Any:
        raise RuntimeError(
            "SUB=private-cookie&state=oauth-state&code=oauth-code&uuid=upstream-uuid"
        )

    if phase == "poll":
        qr = await local.send("POST", PREFIX + "/qr", {})
        monkeypatch.setattr(FakeLogin, "poll", broken)
        result = await local.send("GET", PREFIX + "/poll?login_id=" + qr["login_id"], status=502)
    else:
        monkeypatch.setattr(FakeLogin, "generate", broken)
        result = await local.send("POST", PREFIX + "/qr", {}, 502)
    status = await local.send("GET", PREFIX + "/session")
    assert status["configured"] and status["locally_usable"] and status["valid"] is None
    assert FakeLogin.instances[-1].closed
    assert not any(
        secret in json.dumps([result, status])
        for secret in ("private-cookie", "oauth-state", "oauth-code", "upstream-uuid")
    )


async def test_default_http_available_without_browser(
    local: LocalEnv, fake_login: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    await local.create()
    monkeypatch.setattr(routes_sources_session, "weibo_login_status", weibo_login_status)
    status = await local.send("GET", PREFIX + "/session")
    assert status["available"] and status["method"] == "http_wechat"
    assert status["requires_browser"] is False
    qr = await local.send("POST", PREFIX + "/qr", {})
    assert qr["available"] and qr["method"] == "http_wechat"
    assert UUID(qr["login_id"]).version == 4


@pytest.mark.parametrize("initial_state", ["new", "generating", "waiting", "scanned"])
async def test_all_active_states_refuse_reentry(
    local: LocalEnv, fake_login: None, initial_state: str
) -> None:
    await local.create()
    await local.send("POST", PREFIX + "/qr", {})
    login = FakeLogin.instances[-1]
    login.state = initial_state
    await local.send("POST", PREFIX + "/qr", {}, 409)
    assert len(FakeLogin.instances) == 1 and not login.closed
