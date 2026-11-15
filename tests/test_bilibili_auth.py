from __future__ import annotations

import json

import httpx
import pytest

from jianshanagent.bilibili_auth import BilibiliCredentialStore, BilibiliQrLogin


@pytest.mark.asyncio
async def test_qr_login_reads_sessdata_from_set_cookie(tmp_path, monkeypatch) -> None:
    target = tmp_path / "credential.json"
    monkeypatch.setenv("JIANSHANAGENT_BILIBILI_CREDENTIAL_FILE", str(target))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/generate"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {"url": "https://passport.bilibili.com/qr", "qrcode_key": "key"},
                },
            )
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "url": "https://passport.bilibili.com/exchange?code=only-a-code",
                    "refresh_token": "refresh-token",
                    "code": 0,
                },
            },
            headers=[
                ("set-cookie", "SESSDATA=sess-value; Path=/; Domain=bilibili.com; HttpOnly"),
                ("set-cookie", "bili_jct=jct-value; Path=/"),
                ("set-cookie", "DedeUserID=42; Path=/"),
            ],
        )

    login = BilibiliQrLogin(
        BilibiliCredentialStore(tmp_path), transport=httpx.MockTransport(handler)
    )
    assert (await login.generate()).startswith("data:image/png;base64,")
    assert await login.poll() == "done"

    saved = json.loads(target.read_text())
    assert saved["sessdata"] == "sess-value"
    assert saved["bili_jct"] == "jct-value"
    assert saved["dedeuserid"] == "42"
    assert saved["ac_time_value"] == "refresh-token"
