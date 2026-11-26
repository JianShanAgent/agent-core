"""Offline transport fixtures, NOT evidence of a real person's completed OAuth login."""

from __future__ import annotations

import asyncio
import base64
import io
import logging
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, urlencode

import httpx
import pytest
from PIL import Image

from jianshanagent.weibo_http import WechatHttpError, WechatHttpSession, _poll_result, _safe_url

_STATE = "fixture-private-state"
_UUID = "fixture-private-uuid"
_CODE = "fixture-private-code"
_SUB = "fixture-private-cookie"
_CALLBACK = "https://passport.weibo.com/othersitebind/auth?" + urlencode(
    {
        "site": "weixin",
        "entry": "miniblog",
        "auth_scope": "all",
        "m": "opaque-value",
        "r": "https://weibo.com/",
    }
)
_AUTHORIZE = "https://open.weixin.qq.com/connect/qrconnect?" + urlencode(
    {
        "appid": "official-fixture-app",
        "redirect_uri": _CALLBACK,
        "response_type": "code",
        "scope": "snsapi_login",
        "state": _STATE,
    }
)


def image_bytes(*, size: tuple[int, int] = (128, 128), fmt: str = "PNG") -> bytes:
    output = io.BytesIO()
    Image.new("RGB", size, "white").save(output, format=fmt)
    return output.getvalue()


class OfficialFixture:
    """Synthetic first-party responses; success still requires real-user acceptance."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.polls = ["window.wx_errcode=405;window.wx_code='" + _CODE + "';"]
        self.identity: Any = {"ok": 1, "data": {"login": True}}
        self.callback: httpx.Response | None = None
        self.auth_location = _AUTHORIZE
        self.image = image_bytes()
        self.image_src = "/connect/qrcode/" + _UUID
        self.extra_image_srcs: list[str] = []
        self.config_override: dict[str, Any] | None = None
        self.set_sub = True
        self.on_poll: Callable[[], None] | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host, path = request.url.host, request.url.path
        if host == "passport.weibo.com" and path == "/sso/signin":
            assert parse_qs(request.url.query.decode()) == {
                "entry": ["miniblog"],
                "source": ["miniblog"],
                "url": ["https://weibo.com/"],
            }
            return httpx.Response(
                200,
                text="official signin",
                headers={"Set-Cookie": "X-CSRF-TOKEN=anonymous%2Ftoken; Path=/; Secure"},
            )
        if host == "passport.weibo.com" and path == "/sso/v2/web/config":
            assert request.method == "POST"
            assert request.headers["content-type"] == "application/x-www-form-urlencoded"
            assert request.content == b"entry=miniblog&source=miniblog"
            assert request.headers["x-csrf-token"] == "anonymous/token"
            assert request.headers["origin"] == "https://passport.weibo.com"
            assert request.headers["x-requested-with"] == "XMLHttpRequest"
            assert request.headers["referer"].startswith("https://passport.weibo.com/sso/signin?")
            assert "X-CSRF-TOKEN=" in request.headers["cookie"]
            return httpx.Response(
                200,
                json=self.config_override
                or {
                    "retcode": 20000000,
                    "data": {
                        "show_wechat": 1,
                        "wechat_url": "https://passport.weibo.com/othersitebind/auth?"
                        "site=weixin&entry=miniblog&auth_scope=all",
                    },
                },
            )
        if host == "passport.weibo.com" and path == "/othersitebind/auth":
            query = parse_qs(request.url.query.decode())
            if "code" not in query:
                assert query["r"] == ["https://weibo.com/"]
                return httpx.Response(302, headers={"Location": self.auth_location})
            assert query["code"] == [_CODE]
            assert query["state"] == [_STATE]
            assert query["m"] == ["opaque-value"]
            if self.callback is not None:
                return self.callback
            headers = [("Location", "https://weibo.com/")]
            if self.set_sub:
                sub = f"SUB={_SUB}; Domain=.weibo.com; Path=/; Secure; HttpOnly"
                headers += [("Set-Cookie", sub)]
            return httpx.Response(302, headers=headers)
        if host in {"open.weixin.qq.com", "long.open.weixin.qq.com"}:
            assert "x-csrf-token" not in request.headers
            assert "authorization" not in request.headers
            assert "cookie" not in request.headers or "anonymous" not in request.headers["cookie"]
        if host == "open.weixin.qq.com" and path == "/connect/qrconnect":
            return httpx.Response(
                200,
                text="".join(
                    '<img class="js_qrcode_img extra" src="' + src + '">'
                    for src in [self.image_src, *self.extra_image_srcs]
                ),
                headers={"Set-Cookie": "wx_session=private-wx-cookie; Path=/; Secure"},
            )
        if host == "open.weixin.qq.com" and path.startswith("/connect/qrcode/"):
            return httpx.Response(200, content=self.image, headers={"Content-Type": "image/png"})
        if host == "long.open.weixin.qq.com" and path == "/connect/l/qrconnect":
            assert parse_qs(request.url.query.decode())["uuid"] == [_UUID]
            if self.on_poll:
                self.on_poll()
            return httpx.Response(200, text=self.polls.pop(0))
        if host == "weibo.com" and path == "/":
            return httpx.Response(200, text="<html>Weibo SPA landing page</html>")
        if host == "weibo.com" and path == "/ajax/config/get":
            return httpx.Response(200, json=self.identity)
        raise AssertionError("Unexpected fixture request")


def session(fixture: OfficialFixture, **kwargs: Any) -> WechatHttpSession:
    return WechatHttpSession(httpx.AsyncClient(transport=httpx.MockTransport(fixture)), **kwargs)


async def test_synthetic_success_requires_confirmation_callback_and_identity() -> None:
    fixture = OfficialFixture()
    fixture.polls = [
        "window.wx_errcode=408;",
        "window.wx_errcode=404;",
        "window.wx_errcode=405;window.wx_code='" + _CODE + "';",
    ]
    login = session(fixture)
    data = await login.generate_qr()
    assert data.startswith("data:image/png;base64,")
    assert base64.b64decode(data.split(",", 1)[1]).startswith(b"\x89PNG\r\n\x1a\n")
    assert login.state == "waiting" and not login.verified
    assert await login.cookies() == [] and login.state == "waiting"
    assert await login.cookies() == [] and login.state == "scanned"
    cookies = await login.cookies()
    assert login.verified is True and login.state == "confirmed"
    assert [cookie["name"] for cookie in cookies] == ["SUB"]
    assert cookies[0]["domain"] == ".weibo.com"
    assert cookies[0]["value"] == _SUB
    cookies[0]["value"] = "changed"
    assert (await login.cookies())[0]["value"] == _SUB
    assert _UUID not in repr(login) and _STATE not in repr(login)
    await login.aclose()
    assert login.state == "confirmed" and login.verified
    assert not list(login._client.cookies.jar)
    assert await login.cookies() == []


async def test_responsive_layout_repeated_identical_official_image_is_accepted() -> None:
    fixture = OfficialFixture()
    fixture.extra_image_srcs = [fixture.image_src, "https://open.weixin.qq.com" + fixture.image_src]
    login = session(fixture)
    assert (await login.generate_qr()).startswith("data:image/png;base64,")
    assert login.state == "waiting" and not login.verified
    assert sum(r.url.path.startswith("/connect/qrcode/") for r in fixture.requests) == 1
    await login.aclose()


@pytest.mark.parametrize("extra", ["/connect/qrcode/another-uuid", "https://evil.example/qr"])
async def test_distinct_or_unsafe_qr_images_are_not_silently_selected(extra: str) -> None:
    fixture = OfficialFixture()
    fixture.extra_image_srcs = [extra]
    login = session(fixture)
    with pytest.raises(WechatHttpError):
        await login.generate_qr()
    assert login.state == "error" and not login.verified
    assert not any(r.url.path.startswith("/connect/qrcode/") for r in fixture.requests)
    await login.aclose()


@pytest.mark.parametrize(
    "identity",
    [
        {"ok": -100},
        {"ok": 1, "data": {}},
        {"ok": 1, "data": {"login": False}},
        {"ok": 1, "data": {"login": "true"}},
        {"ok": True, "data": {"login": True}},
        [],
    ],
)
async def test_sub_alone_or_ambiguous_identity_is_never_success(identity: Any) -> None:
    fixture = OfficialFixture()
    fixture.identity = identity
    login = session(fixture)
    await login.generate_qr()
    assert await login.cookies() == []
    assert login.state == "error" and not login.verified
    assert login.error == WechatHttpError("identity").public_message
    await login.aclose()


async def test_rejected_callback_keeps_only_safe_route_and_stage_after_close() -> None:
    fixture = OfficialFixture()
    fixture.callback = httpx.Response(
        302, headers={"Location": "https://weibo.com/sso/logincallback?ticket=" + _SUB}
    )
    login = session(fixture)
    await login.generate_qr()
    assert await login.cookies() == []
    await login.aclose()
    assert login.state == "error" and not login.verified
    assert login.stage == "weibo_callback" and login.error_code == "unsafe"
    assert login.callback_route == "weibo_logincallback" and login.callback_scheme == "https"
    assert not login._callback and not list(login._client.cookies.jar)


async def test_identity_positive_without_sub_rejected() -> None:
    fixture = OfficialFixture()
    fixture.set_sub = False
    login = session(fixture)
    await login.generate_qr()
    assert await login.cookies() == []
    assert not login.verified
    await login.aclose()


@pytest.mark.parametrize(
    "callback",
    [
        httpx.Response(200, text="<form>绑定账号 手机号 验证码</form>"),
        httpx.Response(200, text="<script>location.replace('https://weibo.com/')</script>"),
        httpx.Response(302, headers={"Location": "https://evil.example/?ticket=secret"}),
        httpx.Response(302, headers={"Location": "https://passport.weibo.com/account/bind"}),
        httpx.Response(403, text="risk-control"),
    ],
)
async def test_callback_rejects_binding_challenge_unknown_js_or_redirect(
    callback: httpx.Response,
) -> None:
    fixture = OfficialFixture()
    fixture.callback = callback
    login = session(fixture)
    await login.generate_qr()
    assert await login.cookies() == []
    assert login.state == "error" and not login.verified
    assert not any(r.url.path == "/ajax/config/get" for r in fixture.requests)
    await login.aclose()


@pytest.mark.parametrize(
    "value",
    [
        "http://passport.weibo.com/sso/signin",
        "https://passport.weibo.com.evil/sso/signin",
        "https://user@passport.weibo.com/sso/signin",
        "https://passport.weibo.com:444/sso/signin",
        "https://passport.weibo.com/sso/signin#fragment",
        "https://127.0.0.1/",
        "https://weibo.com/ajax/statuses/buildComments",
        "//evil.example/",
        "https://passport.weibo.com/othersitebind/auth\n",
        "https://weibo.com\\@evil.example/",
        "https://passport.weibo.com/%2e%2e/othersitebind/auth",
    ],
)
def test_strict_first_party_url_allowlist(value: str) -> None:
    with pytest.raises(WechatHttpError, match="安全限制"):
        _safe_url(value)


@pytest.mark.parametrize(
    "body",
    [
        "window.wx_errcode=405;window.wx_code='ok';evil()",
        "window.wx_errcode=405",
        "window.wx_errcode=405;window.wx_code='a\\x62';",
        "alert(1)",
    ],
)
def test_fixed_poll_assignments_only(body: str) -> None:
    with pytest.raises(WechatHttpError):
        _poll_result(body)


@pytest.mark.parametrize(
    "poll",
    [
        "window.wx_errcode=405;",
        "window.wx_errcode=402;",
        "window.wx_errcode=500;",
        "window.wx_errcode=403;",
        "window.wx_errcode=999;",
    ],
)
async def test_unknown_or_missing_confirmation_never_calls_back(poll: str) -> None:
    fixture = OfficialFixture()
    fixture.polls = [poll]
    login = session(fixture)
    await login.generate_qr()
    assert await login.cookies() == []
    assert login.state == "error" and not login.verified
    assert not any("code=" in str(r.url) for r in fixture.requests)
    await login.aclose()


async def test_poll_read_timeout_keeps_scanned_and_then_can_confirm() -> None:
    fixture = OfficialFixture()
    login = session(fixture)
    await login.generate_qr()
    login.state = "scanned"

    def timeout() -> None:
        raise httpx.ReadTimeout("sensitive-original-message " + _STATE)

    fixture.on_poll = timeout
    assert await login.cookies() == []
    assert login.state == "scanned" and not login.error
    fixture.on_poll = None
    assert await login.cookies()
    await login.aclose()


async def test_deadline_expired_and_close_preserves_terminal() -> None:
    fixture = OfficialFixture()
    login = session(fixture)
    await login.generate_qr()
    login._deadline = 0
    assert await login.cookies() == []
    assert login.state == "expired" and not login.verified
    await login.aclose()
    assert login.state == "expired"


async def test_cancelled_poll_propagates_and_close_is_idempotent() -> None:
    fixture = OfficialFixture()
    login = session(fixture)
    await login.generate_qr()

    def cancel() -> None:
        raise asyncio.CancelledError

    fixture.on_poll = cancel
    with pytest.raises(asyncio.CancelledError):
        await login.cookies()
    assert login.state == "cancelled"
    await login.aclose()
    await login.aclose()
    assert login.state == "cancelled" and login._client.is_closed


@pytest.mark.parametrize(
    "image",
    [
        b"not an image",
        image_bytes(size=(32, 32)),
        image_bytes(size=(2200, 100)),
        b"x" * (512 * 1024 + 1),
    ],
)
async def test_bad_or_oversized_qr_rejected(image: bytes) -> None:
    fixture = OfficialFixture()
    fixture.image = image
    login = session(fixture)
    with pytest.raises(WechatHttpError):
        await login.generate_qr()
    assert login.state == "error" and not login.verified
    await login.aclose()


async def test_supported_jpeg_is_converted_to_png() -> None:
    fixture = OfficialFixture()
    fixture.image = image_bytes(fmt="JPEG")
    login = session(fixture)
    data = await login.generate_qr()
    assert base64.b64decode(data.split(",", 1)[1]).startswith(b"\x89PNG")
    await login.aclose()


async def test_foreign_qr_source_rejected_before_request() -> None:
    fixture = OfficialFixture()
    fixture.image_src = "https://evil.example/connect/qrcode/" + _UUID
    login = session(fixture)
    with pytest.raises(WechatHttpError, match="安全限制"):
        await login.generate_qr()
    assert all(r.url.host != "evil.example" for r in fixture.requests)
    await login.aclose()


async def test_domain_expiry_and_partition_filtering() -> None:
    fixture = OfficialFixture()
    login = session(fixture)
    await login.generate_qr()
    client = login._client
    client.cookies.set("bad", "wx", domain="open.weixin.qq.com")
    client.cookies.set("bad", "passport", domain="passport.weibo.com")
    client.cookies.set("bad", "lookalike", domain="evilweibo.com")
    await login.cookies()
    cookies = login._export_cookies()
    assert len(cookies) == 1 and cookies[0]["name"] == "SUB"
    for cookie in client.cookies.jar:
        if cookie.name == "SUB":
            cookie.set_nonstandard_attr("Partitioned", "")
    assert login._export_cookies() == []
    await login.aclose()


@pytest.mark.parametrize("level", [logging.INFO, logging.DEBUG])
async def test_login_http_logs_redact_all_secrets_but_unrelated_logs_remain(
    caplog: pytest.LogCaptureFixture,
    level: int,
) -> None:
    caplog.set_level(level)
    caplog.set_level(level, logger="httpx")
    caplog.set_level(level, logger="httpcore.http11")
    fixture = OfficialFixture()
    login = session(fixture)
    original = fixture.on_poll

    def debug_headers() -> None:
        logging.getLogger("httpcore.http11").debug(
            "headers %s", {"Cookie": _SUB, "state": _STATE, "code": _CODE, "uuid": _UUID}
        )

    fixture.on_poll = debug_headers
    await login.generate_qr()
    await login.cookies()
    await login.aclose()
    for secret in (_STATE, _UUID, _CODE, _SUB, "private-wx-cookie", "anonymous%2Ftoken"):
        assert secret not in caplog.text
    assert "sensitive fields redacted" in caplog.text
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200))
    ) as unrelated:
        await unrelated.get("https://unrelated.example/ordinary?visible=yes")
    assert "unrelated.example/ordinary?visible=yes" in caplog.text
    fixture.on_poll = original


async def test_transport_cleanup_survives_cancelled_close_caller() -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    class SlowClose(httpx.MockTransport):
        async def aclose(self) -> None:
            entered.set()
            await release.wait()

    client = httpx.AsyncClient(transport=SlowClose(OfficialFixture()))
    login = WechatHttpSession(client)
    closing = asyncio.create_task(login.aclose())
    await asyncio.wait_for(entered.wait(), timeout=1)
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing
    assert login._close_task is not None and not login._close_task.done()
    release.set()
    await asyncio.wait_for(login.aclose(), timeout=1)
    assert client.is_closed and login._close_task.done()
    await login.aclose()


async def test_safe_error_and_repr_do_not_include_server_payload() -> None:
    fixture = OfficialFixture()
    fixture.config_override = {"retcode": 1, "message": _SUB + _STATE + _CODE}
    login = session(fixture)
    with pytest.raises(WechatHttpError) as error:
        await login.generate_qr()
    assert error.value.code == "init"
    assert all(
        secret not in str(error.value) + repr(error.value) + repr(login)
        for secret in (_SUB, _STATE, _CODE)
    )
    await login.aclose()


async def test_close_cancels_inflight_poll_and_releases_resources() -> None:
    fixture = OfficialFixture()
    entered = asyncio.Event()
    stopped = asyncio.Event()

    async def transport(request: httpx.Request) -> httpx.Response:
        if request.url.host == "long.open.weixin.qq.com":
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        return fixture(request)

    login = WechatHttpSession(httpx.AsyncClient(transport=httpx.MockTransport(transport)))
    await login.generate_qr()
    polling = asyncio.create_task(login.cookies())
    await entered.wait()
    await asyncio.wait_for(login.aclose(), timeout=1)
    assert stopped.is_set() and polling.cancelled()
    assert login.state == "cancelled" and login._client.is_closed
    assert not login._uuid and not login._oauth_state and not login._callback
    assert not list(login._client.cookies.jar)


async def test_close_cancels_inflight_initialization() -> None:
    entered = asyncio.Event()

    async def transport(request: httpx.Request) -> httpx.Response:
        entered.set()
        await asyncio.Event().wait()
        return httpx.Response(200)

    login = WechatHttpSession(httpx.AsyncClient(transport=httpx.MockTransport(transport)))
    generation = asyncio.create_task(login.generate_qr())
    await entered.wait()
    await asyncio.wait_for(login.aclose(), timeout=1)
    assert generation.cancelled() and login.state == "cancelled"


async def test_callback_read_timeout_is_error_not_retrying_oauth_code() -> None:
    fixture = OfficialFixture()

    def transport(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/othersitebind/auth" and "code=" in str(request.url):
            raise httpx.ReadTimeout("private callback " + _CODE)
        return fixture(request)

    login = WechatHttpSession(httpx.AsyncClient(transport=httpx.MockTransport(transport)))
    await login.generate_qr()
    assert await login.cookies() == []
    assert login.state == "error" and not login.verified
    assert _CODE not in login.error
    assert await login.cookies() == []
    await login.aclose()


async def test_injected_default_auth_and_sensitive_headers_are_not_forwarded() -> None:
    fixture = OfficialFixture()

    def transport(request: httpx.Request) -> httpx.Response:
        assert "authorization" not in request.headers
        assert "x-xsrf-token" not in request.headers
        assert "injected-secret" not in request.headers.get("cookie", "")
        return fixture(request)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(transport),
        auth=("injected-user", "injected-password"),
        headers={
            "X-CSRF-TOKEN": "injected-secret",
            "X-XSRF-TOKEN": "injected-secret",
            "Cookie": "secret=injected-secret",
        },
    )
    client.cookies.set("SUB", "previous-identity", domain=".weibo.com")
    login = WechatHttpSession(client)
    assert not list(client.cookies.jar)
    await login.generate_qr()
    assert await login.cookies()
    await login.aclose()


@pytest.mark.parametrize(
    "location",
    [
        "https://passport.weibo.com/othersitebind/auth?loop=yes",
        "//evil.example/connect/qrconnect",
        "https://open.weixin.qq.com/connect/qrcode/badid1",
    ],
)
async def test_authorization_redirect_loop_or_non_authorization_target_rejected(
    location: str,
) -> None:
    fixture = OfficialFixture()
    fixture.auth_location = location
    login = session(fixture)
    with pytest.raises(WechatHttpError):
        await login.generate_qr()
    assert login.state == "error"
    assert len(fixture.requests) <= 11
    assert all(r.url.host != "evil.example" for r in fixture.requests)
    await login.aclose()


async def test_csrf_cookie_is_required() -> None:
    fixture = OfficialFixture()

    def transport(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/sso/signin":
            return httpx.Response(200, text="signin without CSRF")
        return fixture(request)

    login = WechatHttpSession(httpx.AsyncClient(transport=httpx.MockTransport(transport)))
    with pytest.raises(WechatHttpError) as error:
        await login.generate_qr()
    assert error.value.code == "init"
    assert not fixture.requests
    await login.aclose()


async def test_expired_cookies_are_not_exported() -> None:
    fixture = OfficialFixture()
    login = session(fixture)
    await login.generate_qr()
    await login.cookies()
    for cookie in login._client.cookies.jar:
        if cookie.name == "SUB":
            cookie.expires = 1
    assert login._export_cookies() == []
    await login.aclose()

    def test_official_wechat_redirect_anchor_is_stripped_only_for_qr_page() -> None:
        target = "https://open.weixin.qq.com/connect/qrconnect?fixture=1"
        assert _safe_url(target + "#wechat_redirect") == target
        for rejected in (
            target + "#unknown",
            "https://passport.weibo.com/othersitebind/auth#wechat_redirect",
            "https://open.weixin.qq.com/connect/qrcode/fixture-id#wechat_redirect",
        ):
            with pytest.raises(WechatHttpError):
                _safe_url(rejected)
