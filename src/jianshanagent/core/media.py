"""按地址把用户发来的图片下载成字节。

地址是外面给的（QQ 平台的附件地址），所以下载时守住三条，任何一条不满足都抛
:class:`MediaError`，调用方当成「这张图没取到」：

- 只走 https，证书照常校验。
- 不连内网（防 SSRF）：域名先解析，解析结果里只要有一个不是公网地址（回环、局域网、
  链路本地、云主机的元数据地址……）就不下。重定向不让 httpx 自动跟：每跳一次都按同样的
  规则重新检查，最多跳 ``max_redirects`` 次。
- 不无限收、不无限等：边收边数，超过 ``max_bytes`` 立刻断开；整个下载（解析域名、
  各次跳转、收数据）加起来不超过 ``timeout_seconds``。

没有用 SDK 的 AttachmentDownloader：它只检查地址是不是 http(s) 开头（内网地址照连）、
不限大小、还会把文件存进磁盘上的缓存目录，这里只要内存里的字节。

已知的缺口：检查时解析一次域名，httpx 连接时还会再解析一次，两次之间解析结果被换掉
（DNS 重绑定）是防不住的。地址来自 QQ 平台，域名不由用户控制，所以没有把连接钉在检查过的
地址上——钉住的话，走代理的环境里证书校验会失败。
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Sequence

import httpx

DEFAULT_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 10.0

#: 域名 → 解析出来的地址。测试里换成不联网的替身
Resolver = Callable[[str], Awaitable[Sequence[str]]]

# 代理软件的 fake-ip 模式（本机调试时常开着）会把所有域名都解析到这两个「性能测试专用」
# 网段，真正的连接由代理去建。它们在公网上不存在，一般的内网也不用，所以不当成内网地址；
# 拦了的话，开着这种代理的机器上一张图都下不了。
_FAKE_IP_NETWORKS = (
    ipaddress.ip_network("198.18.0.0/15"),
    ipaddress.ip_network("2001:2::/48"),
)


class MediaError(RuntimeError):
    """图片没取到：地址不让下、下载出错、太大或超时。"""


def is_public_address(address: str) -> bool:
    """是不是可以连的公网地址（回环、局域网、链路本地、保留地址都不是）。"""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])  # fe80::1%en0 这种带网卡名的写法
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped  # ::ffff:10.0.0.1 按里面的 IPv4 地址算
    return ip.is_global or any(ip in network for network in _FAKE_IP_NETWORKS)


async def _system_resolve(host: str) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


def _brief(url: str) -> str:
    """写进报错和日志的地址：不带参数（QQ 图片地址的参数里有访问凭据）。"""
    return url.split("?", 1)[0][:120]


class MediaFetcher:
    def __init__(
        self,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_redirects: int = 3,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: Resolver | None = None,
    ):
        self.max_bytes = max_bytes
        self.timeout_seconds = timeout_seconds
        self.max_redirects = max_redirects
        self._resolve = resolver or _system_resolve
        self._client = httpx.AsyncClient(
            timeout=timeout_seconds, follow_redirects=False, transport=transport
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def fetch(self, url: str) -> bytes:
        """下载一张图片，返回原始字节。取不到抛 :class:`MediaError`。"""
        try:
            async with asyncio.timeout(self.timeout_seconds):
                return await self._download(url)
        except TimeoutError as exc:
            raise MediaError(f"{self.timeout_seconds:g} 秒内没有下完：{_brief(url)}") from exc
        except (httpx.HTTPError, httpx.InvalidURL, OSError, ValueError) as exc:
            # 网络出错、地址写得不对、域名解析不了（含域名不合法时的 UnicodeError）
            reason = f"{type(exc).__name__}: {exc}"[:200]
            raise MediaError(f"下载失败：{_brief(url)}（{reason}）") from exc

    async def _download(self, url: str) -> bytes:
        for _ in range(self.max_redirects + 1):
            await self._check(url)
            async with self._client.stream("GET", url) as response:
                if response.has_redirect_location:
                    url = str(response.url.join(response.headers["location"]))
                    continue
                if response.status_code != 200:
                    raise MediaError(f"HTTP {response.status_code}：{_brief(url)}")
                declared = response.headers.get("content-length", "")
                if declared.isdigit() and int(declared) > self.max_bytes:
                    raise MediaError(f"图片有 {declared} 字节，超过上限：{_brief(url)}")
                received = bytearray()
                async for chunk in response.aiter_bytes():
                    received += chunk
                    if len(received) > self.max_bytes:  # 没报大小、或者报小了的
                        raise MediaError(f"图片超过 {self.max_bytes} 字节的上限：{_brief(url)}")
                return bytes(received)
        raise MediaError(f"跳转超过 {self.max_redirects} 次：{_brief(url)}")

    async def _check(self, url: str) -> None:
        """这个地址能不能连：只许 https，主机解析出来的必须全是公网地址。"""
        parsed = httpx.URL(url)
        if parsed.scheme != "https" or not parsed.host:
            raise MediaError(f"只下载 https 地址：{_brief(url)}")
        addresses = await self._resolve(parsed.host)
        internal = [address for address in addresses if not is_public_address(address)]
        if internal or not addresses:
            where = internal[0] if internal else "空"
            raise MediaError(f"{parsed.host} 解析到 {where}，不是公网地址：{_brief(url)}")
