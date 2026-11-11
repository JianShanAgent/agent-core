"""对 wiki 的只读访问：限速、重试、请求计数，以及按「页面 ID + 修订号」落盘的原始内容缓存。

对服务器客气的几道保证：

- 并发不超过 2（信号量），整体不超过每秒 2 个请求（相邻两次请求的发出时刻至少隔 0.5 秒）；
- 取到的原始内容都写进 ``data/kb/cache/``，键是页面 ID + 修订号，重跑时命中缓存就不再请求；
- 请求失败只对「网络错误 / 429 / 5xx」退避重试；
  被 Cloudflare 拦下（返回的不是 JSON）立刻报错，不反复撞；
- 没人盯着的任务（自动更新）另有两道：一次运行的请求数有上限（``budget``），发满了就不再发，
  后面的请求直接报 :class:`BudgetExceeded`——不能因为哪里判断错了就一直发下去；
  连着几个请求都没成（``give_up_after``）就整个收手，后面的直接报 :class:`GaveUp`——
  wiki 出故障的时候，不能每个页面都各自重试到底，在它最吃力的时候还不停地敲。
"""

from __future__ import annotations

import asyncio
import gzip
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from jianshanagent.core.logging import get_logger
from jianshanagent.plugins.knowledge.settings import FetchSettings, WikiSettings

logger = get_logger(__name__)


class FetchError(RuntimeError):
    """wiki 请求失败（被拦截、接口报错、重试用尽）。"""


class BudgetExceeded(FetchError):
    """这次运行的请求数已经到了上限，这个请求没有发出去。"""


class GaveUp(FetchError):
    """前面连着好几个请求都没成，这次运行不再发请求了；这个请求没有发出去。"""


@dataclass(slots=True)
class FetchStats:
    requests: int = 0
    retries: int = 0
    bytes_received: int = 0
    cache_hits: int = 0
    by_kind: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "requests": self.requests,
            "retries": self.retries,
            "bytes_received": self.bytes_received,
            "cache_hits": self.cache_hits,
            "by_kind": dict(self.by_kind),
        }


class RateLimiter:
    """保证相邻两次请求的发出时刻至少相隔 ``1 / rps`` 秒。"""

    def __init__(self, requests_per_second: float):
        self._interval = 1.0 / requests_per_second
        self._lock = asyncio.Lock()
        self._next_at = 0.0

    async def wait(self) -> None:
        async with self._lock:
            loop = asyncio.get_running_loop()
            delay = self._next_at - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            self._next_at = max(loop.time(), self._next_at) + self._interval


class DiskCache:
    """原始内容缓存：``<cache_dir>/<类别>/<页面ID>-<修订号>.json.gz``。"""

    def __init__(self, root: Path):
        self.root = root

    def _path(self, kind: str, page_id: int, rev_id: int) -> Path:
        return self.root / kind / f"{page_id}-{rev_id}.json.gz"

    def get(self, kind: str, page_id: int, rev_id: int) -> Any | None:
        path = self._path(kind, page_id, rev_id)
        if not path.exists():
            return None
        try:
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError, EOFError):
            # 写到一半被打断的残缺文件：当作没有缓存，重新取
            logger.warning("kb_cache_corrupt", path=str(path))
            return None

    def put(self, kind: str, page_id: int, rev_id: int, value: Any) -> None:
        """存一份，并把这个页面更早的修订删掉：新的一到手，旧的就再也用不上了。

        不删的话，天天在改的文档（公告的月合集）每改一次就多留一份，自动更新跑久了会越攒越多。
        """
        path = self._path(kind, page_id, rev_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        with gzip.open(tmp, "wt", encoding="utf-8") as fh:
            json.dump(value, fh, ensure_ascii=False)
        os.replace(tmp, path)  # 原子替换，中断不会留下半个文件
        for rev, older in self._revisions(kind, page_id):
            if rev < rev_id:
                older.unlink(missing_ok=True)

    def _revisions(self, kind: str, page_id: int) -> list[tuple[int, Path]]:
        """这个页面缓存着的各个修订：（修订号，文件）。"""
        found: list[tuple[int, Path]] = []
        for path in (self.root / kind).glob(f"{page_id}-*.json.gz"):
            rev = path.name[len(str(page_id)) + 1 : -len(".json.gz")]
            if rev.isdigit():
                found.append((int(rev), path))
        return found

    def latest(self, kind: str, page_id: int) -> Any | None:
        """这个页面缓存过的最新一版（不管现在的修订号是多少）。

        只读缓存的来源用它；新的一版取不下来时，也拿它顶着（见 sources.py）。
        """
        revisions = self._revisions(kind, page_id)
        if not revisions:
            return None
        return self.get(kind, page_id, max(revisions)[0])

    def stored_at(self, kind: str, page_id: int, rev_id: int) -> float | None:
        """这一份是什么时候存的（文件的修改时间）；没有这一份返回 None。"""
        try:
            return self._path(kind, page_id, rev_id).stat().st_mtime
        except OSError:
            return None

    def get_named(self, name: str) -> Any | None:
        path = self.root / name
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def put_named(self, name: str, value: Any) -> None:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)


class WikiFetcher:
    def __init__(
        self,
        wiki: WikiSettings,
        fetch: FetchSettings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        budget: int | None = None,
        give_up_after: int | None = None,
    ):
        self.wiki = wiki
        self.settings = fetch
        #: 这个对象一共最多发多少个请求（重试也算）；None = 不限
        self.budget = budget
        #: 连着这么多个请求（各自重试完）都没成，就不再发了；None = 不管，每个请求照常试
        self.give_up_after = give_up_after
        self._failed_in_a_row = 0
        self.stats = FetchStats()
        headers = {"User-Agent": wiki.user_agent, "Accept": "application/json"}
        if wiki.auth_header and wiki.auth_key:
            headers[wiki.auth_header] = wiki.auth_key  # Cloudflare 放行请求头
        if wiki.cookie:
            headers["Cookie"] = wiki.cookie
        self._client = httpx.AsyncClient(
            timeout=fetch.timeout_seconds,
            headers=headers,
            follow_redirects=True,
            transport=transport,
        )
        self._semaphore = asyncio.Semaphore(fetch.max_concurrency)
        self._limiter = RateLimiter(fetch.requests_per_second)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get_json(self, kind: str, url: str, params: dict[str, str]) -> Any:
        """对这个站发一个只读请求，返回解析好的 JSON。限速、重试、两道闸都在这里面。

        ``api.php`` 之外的接口（站点插件自己的数据接口）也从这里走，才算在同一份限速和请求数里。
        """
        try:
            data = await self._attempt(kind, url, params)
        except (BudgetExceeded, GaveUp):
            raise  # 这两种是没有发出去，不算 wiki 那边没成
        except FetchError:
            self._failed_in_a_row += 1
            raise
        self._failed_in_a_row = 0
        return data

    async def _attempt(self, kind: str, url: str, params: dict[str, str]) -> Any:
        """发一个请求（失败了按规矩重试），返回解析好的 JSON。"""
        last_error = ""
        for attempt in range(self.settings.max_retries + 1):
            if attempt:
                self.stats.retries += 1
                await asyncio.sleep(min(30.0, 2.0**attempt))
            async with self._semaphore:
                # 两道闸都在轮到自己发的时候才看：排着队等的请求，等到的时候情况可能已经变了
                if self.budget is not None and self.stats.requests >= self.budget:
                    raise BudgetExceeded(f"这次运行已经发了 {self.budget} 个请求，到上限了")
                if self.give_up_after is not None and self._failed_in_a_row >= self.give_up_after:
                    raise GaveUp(
                        f"wiki 连着 {self._failed_in_a_row} 个请求都没成，这次运行不再发请求了"
                    )
                # 先记数再等限速：等的这一会儿另一个并发的请求也会来看这道闸，
                # 等完再记的话两个都会放行，实际发出去的比上限多一个
                self.stats.requests += 1
                self.stats.by_kind[kind] = self.stats.by_kind.get(kind, 0) + 1
                await self._limiter.wait()
                try:
                    resp = await self._client.get(url, params=params)
                except httpx.HTTPError as exc:
                    last_error = repr(exc)
                    continue
            self.stats.bytes_received += len(resp.content)
            if resp.status_code == 429 or resp.status_code >= 500:
                last_error = f"HTTP {resp.status_code}"
                retry_after = resp.headers.get("retry-after", "")
                if retry_after.isdigit():
                    await asyncio.sleep(min(60.0, float(retry_after)))
                continue
            if "json" not in resp.headers.get("content-type", ""):
                # 没带对放行请求头时，Cloudflare 返回的是 HTML 挑战页；重试没有意义
                snippet = resp.text[:120].replace("\n", " ")
                raise FetchError(f"wiki 返回非 JSON（HTTP {resp.status_code}）：{snippet}")
            if resp.status_code >= 400:
                raise FetchError(f"wiki 接口报错（HTTP {resp.status_code}）：{resp.text[:200]}")
            return resp.json()
        raise FetchError(f"重试 {self.settings.max_retries} 次后仍失败：{last_error}")

    async def api(self, kind: str, params: dict[str, str]) -> dict[str, Any]:
        """调 ``api.php``；接口层面的 error 抛 :class:`FetchError`。"""
        data = await self.get_json(
            kind, self.wiki.api_url, {"format": "json", "formatversion": "2", **params}
        )
        if "error" in data:
            error = data["error"]
            raise FetchError(f"wiki API 错误：{error.get('code')}: {error.get('info')}")
        return dict(data)
