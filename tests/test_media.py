"""图片下载：只走 https、不连内网（跳转也算）、限大小、限时间。全程不联网。"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import httpx
import pytest

from jianshanagent.core.media import MediaError, MediaFetcher

# 域名解析的替身。没登记的原样返回：地址写的就是 IP 时，解析结果是它自己
ADDRESSES = {
    "img.example": ["119.147.0.10"],
    "cdn.example": ["198.18.0.134"],  # 代理软件 fake-ip 模式给的地址：不算内网
    "inside.example": ["10.0.0.8"],
    "mixed.example": ["119.147.0.10", "127.0.0.1"],
}

Handler = Callable[[httpx.Request], httpx.Response | Awaitable[httpx.Response]]


async def _resolve(host: str) -> list[str]:
    return ADDRESSES.get(host, [host])


def _fetcher(handler: Handler, **settings: Any) -> MediaFetcher:
    return MediaFetcher(transport=httpx.MockTransport(handler), resolver=_resolve, **settings)


async def test_download_follows_redirects_one_checked_hop_at_a_time() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.host == "img.example":
            return httpx.Response(302, headers={"location": "//cdn.example/a.png?rkey=1"})
        return httpx.Response(200, content=b"image bytes")

    fetcher = _fetcher(handler)
    assert await fetcher.fetch("https://img.example/download?fileid=1") == b"image bytes"
    await fetcher.aclose()
    assert seen == ["https://img.example/download?fileid=1", "https://cdn.example/a.png?rkey=1"]


@pytest.mark.parametrize(
    "url",
    [
        "http://img.example/a.png",  # 不是 https
        "https://10.0.0.8/a.png",  # 局域网
        "https://[::1]/a.png",  # 本机
        "https://169.254.169.254/latest/meta-data",  # 云主机的元数据地址
        "https://inside.example/a.png",  # 域名解析到内网
        "https://mixed.example/a.png",  # 解析结果里混着一个内网地址
    ],
)
async def test_plain_http_and_internal_addresses_are_refused_without_any_request(url: str) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=b"secret")

    fetcher = _fetcher(handler)
    with pytest.raises(MediaError):
        await fetcher.fetch(url)
    await fetcher.aclose()
    assert requests == []


@pytest.mark.parametrize(
    ("location", "requests_made"),
    [
        ("https://inside.example/secret", 1),  # 跳到内网：不跟
        ("http://img.example/a.png", 1),  # 跳到 http：不跟
        ("https://img.example/again", 4),  # 一直跳：跟 3 次就不跟了
    ],
)
async def test_redirects_are_checked_like_the_first_address(
    location: str, requests_made: int
) -> None:
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        return httpx.Response(302, headers={"location": location})

    fetcher = _fetcher(handler)
    with pytest.raises(MediaError):
        await fetcher.fetch("https://img.example/a.png")
    await fetcher.aclose()
    assert hosts == ["img.example"] * requests_made


@pytest.mark.parametrize(
    ("path", "problem"),
    [
        ("/declared-too-big", "超过上限"),  # 响应头报的大小就超了：不收
        ("/endless", "上限"),  # 没报大小、源源不断：收到上限就断开
        ("/slow", "没有下完"),
        ("/expired", "HTTP 400"),  # QQ 的图片地址过期时就是这样回的
        ("/broken", "下载失败"),
    ],
)
async def test_oversized_slow_and_failed_downloads_raise(path: str, problem: str) -> None:
    async def endless() -> AsyncIterator[bytes]:
        while True:
            yield b"x" * 512

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/declared-too-big":
            return httpx.Response(200, content=b"x" * 5000)
        if request.url.path == "/endless":
            return httpx.Response(200, content=endless())
        if request.url.path == "/slow":
            await asyncio.sleep(5)
        if request.url.path == "/expired":
            return httpx.Response(400, json={"retcode": -5503011, "retmsg": "invalid rkey"})
        raise httpx.ConnectError("connection reset", request=request)

    fetcher = _fetcher(handler, max_bytes=2000, timeout_seconds=0.2)
    with pytest.raises(MediaError, match=problem) as info:
        await fetcher.fetch(f"https://img.example{path}?rkey=credential")
    await fetcher.aclose()
    assert "credential" not in str(info.value)  # 地址参数里的访问凭据不进报错和日志
