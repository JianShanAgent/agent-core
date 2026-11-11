"""图片索引的第一步：选图、生成记录、把原图下载到本地缓存。

选哪些图、每张图是什么（记录里的名字和说明）因站而异，由站点插件决定（``image_catalog``，
见 site.py；阴阳师 wiki 的规则在 ``sites/yys/images.py``）。这里是和站点无关的部分：
列 wiki 上的文件、读写记录文件、下载原图。

每张图一条记录（``images.jsonl`` 的一行，字段见 image_store.KbImage）。``subject`` 相同的图
画的是同一个东西，以图搜图的结果里只留最像的一张。

可以反复跑：原图按内容哈希（wiki 给的 sha1）缓存在 ``<缓存目录>/images/files/``，
wiki 上换了图哈希就变，只下新增和变了的。图片常常在另一个域名的 CDN 上，下载不带 wiki 的放行密钥，
速度和并发同样受 fetch 配置的上限约束。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from jianshanagent.core.logging import get_logger
from jianshanagent.plugins.knowledge.fetcher import FetchError, RateLimiter, WikiFetcher
from jianshanagent.plugins.knowledge.image_store import KbImage
from jianshanagent.plugins.knowledge.settings import FetchSettings, Settings, WikiSettings
from jianshanagent.plugins.knowledge.sources import WikiSource

logger = get_logger(__name__)

IMAGES_FILE = "images.jsonl"
#: 原图缓存，在缓存目录下
ORIGINALS_DIR = "images/files"

_FILE_PROPS = "url|size|sha1|mime|timestamp"


@dataclass(frozen=True, slots=True)
class WikiFile:
    """wiki 上的一个文件。``sha1`` 是内容的哈希，换了图它就变。"""

    name: str
    url: str
    sha1: str
    size: int = 0
    width: int = 0
    height: int = 0
    mime: str = ""
    timestamp: str = ""


@dataclass(slots=True)
class Catalog:
    """选出来的图片记录。"""

    images: list[KbImage] = field(default_factory=list)
    #: 选图过程的统计（存成 ``images_stats.json``）
    stats: dict[str, Any] = field(default_factory=dict)
    #: 要告诉跑命令的人的话（没收的、名字待补的……），一条一句
    notes: list[str] = field(default_factory=list)
    #: 这份记录不该用的原因（它依据的数据没取齐）；没问题是空的。
    #: 自动更新看到它就不写出去，手动跑的只提醒
    problem: str = ""

    def summary(self) -> dict[str, Any]:
        """每个图库都有的几个数：多少张、按类别各多少、画的是多少个不同的东西。"""
        return {
            "images": len(self.images),
            "by_kind": dict(Counter(i.kind for i in self.images).most_common()),
            "subjects": len({i.group for i in self.images}),
        }


async def collect_catalog(source: WikiSource, settings: Settings) -> Catalog | None:
    """让管图库的站点插件选图、生成记录。没有插件管图库返回 None。

    不止一个插件管图库时，记录接在一起，统计按插件的名字分开放。
    """
    found: list[tuple[str, Catalog]] = []
    for site in settings.sites:
        catalog = await site.image_catalog(source, settings)
        if catalog is not None:
            found.append((site.id, catalog))
    if not found:
        return None
    if len(found) == 1:
        return found[0][1]
    merged = Catalog()
    for name, catalog in found:
        merged.images += catalog.images
        merged.stats[name] = catalog.stats
        merged.notes += catalog.notes
        merged.problem = merged.problem or catalog.problem
    merged.stats |= merged.summary()
    return merged


# ── 列文件 ────────────────────────────────────────────────────────────


def _wiki_file(name: str, info: dict[str, Any]) -> WikiFile | None:
    if not info.get("url") or not info.get("sha1"):
        return None
    return WikiFile(
        name=name,
        url=str(info["url"]),
        sha1=str(info["sha1"]),
        size=int(info.get("size") or 0),
        width=int(info.get("width") or 0),
        height=int(info.get("height") or 0),
        mime=str(info.get("mime") or ""),
        timestamp=str(info.get("timestamp") or ""),
    )


async def list_files(fetcher: WikiFetcher, prefix: str) -> dict[str, WikiFile]:
    """wiki 上名字以 ``prefix`` 开头的全部文件。"""
    params = {
        "action": "query",
        "list": "allimages",
        "aiprefix": prefix,
        "ailimit": "500",
        "aiprop": _FILE_PROPS,
    }
    files: dict[str, WikiFile] = {}
    cont: dict[str, str] = {}
    while True:
        data = await fetcher.api("list", {**params, **cont})
        for item in (data.get("query") or {}).get("allimages") or []:
            file = _wiki_file(str(item.get("name") or ""), item)
            if file is not None and file.name:
                files[file.name] = file
        if "continue" not in data:
            return files
        cont = {k: str(v) for k, v in data["continue"].items()}


async def lookup_files(fetcher: WikiFetcher, names: Sequence[str]) -> dict[str, WikiFile]:
    """按文件名查（没有共同前缀的文件用它）；wiki 上没有的不在结果里。"""
    files: dict[str, WikiFile] = {}
    for start in range(0, len(names), 50):
        data = await fetcher.api(
            "list",
            {
                "action": "query",
                "titles": "|".join(f"File:{name}" for name in names[start : start + 50]),
                "prop": "imageinfo",
                "iiprop": _FILE_PROPS,
            },
        )
        for page in (data.get("query") or {}).get("pages") or []:
            infos = page.get("imageinfo") or []
            # 接口返回的标题带着命名空间、空格代替了下划线：「文件:雪幽魂 s.png」
            name = str(page.get("title") or "").partition(":")[2].replace(" ", "_")
            file = _wiki_file(name, infos[0]) if infos and name else None
            if file is not None:
                files[file.name] = file
    return files


# ── 记录文件 ──────────────────────────────────────────────────────────


def write_images(path: Path, images: Iterable[KbImage]) -> None:
    """先写临时文件再原子替换：中断的运行不会留下半份记录。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as out:
        for image in images:
            out.write(json.dumps(image.to_json(), ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def read_images(path: Path) -> list[KbImage]:
    with path.open(encoding="utf-8") as lines:
        return [KbImage.from_json(json.loads(line)) for line in lines if line.strip()]


# ── 下载原图 ──────────────────────────────────────────────────────────


def original_path(cache_dir: Path, image: KbImage) -> Path:
    """原图在缓存里的位置：按内容哈希存，扩展名照 wiki 上的文件名。"""
    return cache_dir / ORIGINALS_DIR / f"{image.sha1}{Path(image.image_id).suffix.lower()}"


@dataclass(slots=True)
class DownloadReport:
    #: 要有的原图数（内容一样的算一张）
    total: int = 0
    #: 开始前就已经在缓存里的
    cached: int = 0
    downloaded: int = 0
    bytes_received: int = 0
    requests: int = 0
    failed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    seconds: float = 0.0


async def download_originals(
    images: Sequence[KbImage],
    cache_dir: Path,
    wiki: WikiSettings,
    fetch: FetchSettings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> DownloadReport:
    """把还没缓存的原图下载下来。内容哈希和 wiki 给的对不上的不存（当作下载失败）。"""
    started = time.perf_counter()
    unique = {image.sha1: image for image in images}  # 内容一样的图只下一次
    pending = [image for image in unique.values() if not original_path(cache_dir, image).exists()]
    report = DownloadReport(total=len(unique), cached=len(unique) - len(pending))
    if not pending:
        return report
    (cache_dir / ORIGINALS_DIR).mkdir(parents=True, exist_ok=True)
    gate = asyncio.Semaphore(fetch.max_concurrency)
    limiter = RateLimiter(fetch.requests_per_second)
    # 图片在 CDN 上，是另一个域名：只带 User-Agent，不带 wiki 的放行密钥
    client = httpx.AsyncClient(
        timeout=fetch.timeout_seconds,
        headers={"User-Agent": wiki.user_agent},
        follow_redirects=True,
        transport=transport,
    )

    async def fetch_one(image: KbImage) -> bytes:
        error = ""
        for attempt in range(fetch.max_retries + 1):
            if attempt:
                await asyncio.sleep(min(30.0, 2.0**attempt))
            async with gate:
                await limiter.wait()
                report.requests += 1
                try:
                    resp = await client.get(image.image_url)
                except httpx.HTTPError as exc:
                    error = repr(exc)
                    continue
            if resp.status_code == 429 or resp.status_code >= 500:
                error = f"HTTP {resp.status_code}"
                continue
            if resp.status_code != 200:
                raise FetchError(f"HTTP {resp.status_code}")
            if hashlib.sha1(resp.content).hexdigest() != image.sha1:
                error = "内容哈希和 wiki 给的对不上"  # CDN 上可能还是旧图，过一会儿再试
                continue
            return resp.content
        raise FetchError(f"重试 {fetch.max_retries} 次后仍失败：{error}")

    async def one(image: KbImage) -> None:
        try:
            content = await fetch_one(image)
        except FetchError as exc:
            report.failed.append(image.image_id)
            report.errors.append(f"{image.image_id}：{exc}")
            logger.warning("kb_image_download_failed", image=image.image_id, error=str(exc))
            return
        path = original_path(cache_dir, image)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(content)
        os.replace(tmp, path)
        report.downloaded += 1
        report.bytes_received += len(content)
        if report.downloaded % 100 == 0 or report.downloaded + len(report.failed) == len(pending):
            logger.info(
                "kb_image_download_progress",
                done=report.downloaded,
                failed=len(report.failed),
                total=len(pending),
                megabytes=round(report.bytes_received / 1e6, 1),
            )

    try:
        await asyncio.gather(*(one(image) for image in pending))
    finally:
        await client.aclose()
    report.seconds = time.perf_counter() - started
    return report
