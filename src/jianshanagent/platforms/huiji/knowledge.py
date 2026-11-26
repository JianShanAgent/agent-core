"""灰机平台管知识库的那一摊：灰机 wiki（huijiwiki.com）在 MediaWiki 之上多出来的东西。

- Data 命名空间：站点把结构化数据存成 ``Data:xxx.json`` 页面，条目页用模块读它来渲染。
  内容不走 ``api.php``，走平台的查询接口 ``<站点>/api/rest_v1/namespace/data``
  （按 MongoDB 过滤条件查，``_id`` 是不带命名空间、空格写成下划线的标题）。
  取下来的文档和页面一样按「页面 ID + 修订号」缓存，新的一版取不下来时沿用旧的。
- 清洗的约定：``kb-ignore``（灰机官方知识库的约定：带这个 class 的不进知识库）、
  ``huiji-tt-preload``（悬浮提示的预载内容）。这两条写在 ``platform.huiji`` 的
  ``extend`` 段里，读配置时并进 ``plugin.knowledge`` 的清洗规则。
- 自动更新：Data 文档改了，用它渲染的条目页既没有新修订、``touched`` 也多半不变。
  平台不知道某个站的哪份数据对应哪些页面，所以一律当作「公共数据改了」，
  由自动更新过后把渲染的页面全部重取一遍。知道对应关系的站点插件（``huiji_yys``）排在后面，会先被问到。

站点插件要取 Data 文档时用这里的 :meth:`HuijiKnowledge.data_doc`。
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from jianshanagent.core.logging import get_logger
from jianshanagent.plugins.knowledge.changes import Change, ChangeContext, Plan
from jianshanagent.plugins.knowledge.fetcher import FetchError, WikiFetcher
from jianshanagent.plugins.knowledge.settings import WikiSettings
from jianshanagent.plugins.knowledge.site import Classifier, SitePlugin, SiteSetup, Verdict
from jianshanagent.plugins.knowledge.sources import Fetched, PageRef, Stale, WikiSource

logger = get_logger(__name__)

#: 每批最多查 50 份，串行发出；所有请求仍走 fetcher 的限速和预算
_DATA_BATCH_CAP = 50
#: 中文路径经过百分号编码会膨胀，给常见的 8KB 请求行限制留余量
_DATA_URL_BYTES_CAP = 6000

#: Data 文档在缓存里、请求计数里算哪一类
DATA_KIND = "data"
LABEL_DATA = "Data"


@dataclass(frozen=True, slots=True)
class HuijiSettings:
    #: Data 命名空间的编号
    data_namespace: int = 3500
    #: Data 查询接口在站内的地址
    data_path: str = "/api/rest_v1/namespace/data"


class HuijiKnowledge(SitePlugin):
    id = "huiji"

    def __init__(self, cfg: HuijiSettings | None = None):
        self.cfg = cfg or HuijiSettings()

    @property
    def data_namespace(self) -> int:
        return self.cfg.data_namespace

    def data_url(self, wiki: WikiSettings) -> str:
        return f"{wiki.site_base}{self.cfg.data_path}"

    async def data_find(
        self, fetcher: WikiFetcher, filter_: dict[str, Any], **params: str
    ) -> list[dict[str, Any]]:
        """按 MongoDB 过滤条件查 Data 命名空间。"""
        data = await fetcher.get_json(
            DATA_KIND,
            self.data_url(fetcher.wiki),
            {"filter": json.dumps(filter_, ensure_ascii=False), **params},
        )
        if not isinstance(data, dict):
            raise FetchError("Data 接口返回的不是对象")
        docs = data.get("_embedded")
        if docs is None:
            return []
        if not isinstance(docs, list):
            raise FetchError("Data 接口的 _embedded 不是列表")
        return docs

    async def data_many(
        self,
        source: WikiSource,
        refs: Sequence[PageRef],
        *,
        label: str = "Data",
        batch_size: int = 50,
    ) -> list[Fetched]:
        """按输入顺序返回 Data 文档，只把当前修订未缓存的部分分批串行查询。

        按完整 ``_id`` 对齐，不能按返回次序或文件名认文档。失败、缺项或重复 ID
        都只回退旧缓存，不逐条补请求，也不把缺项当删除。离线只重放当前修订。
        """
        batch_size = max(1, min(batch_size, _DATA_BATCH_CAP))
        results: list[Fetched] = []
        missing: dict[str, list[int]] = {}
        done = 0

        def completed() -> None:
            nonlocal done
            done += 1
            if done % 100 == 0 or done == len(refs):
                logger.info("kb_fetch_progress", what=label, done=done, total=len(refs))

        for ref in refs:
            cached = source.cached(DATA_KIND, ref)
            results.append(Fetched(ref, value=dict(cached) if cached is not None else None))
            if cached is None:
                missing.setdefault(ref.data_id, []).append(len(results) - 1)
            else:
                completed()

        def url_size(data_ids: list[str]) -> int:
            params = {
                "filter": json.dumps({"_id": {"$in": data_ids}}, ensure_ascii=False),
                "limit": str(batch_size),
            }
            return len(f"{self.data_url(source.settings.wiki)}?{urlencode(params)}".encode())

        def batches() -> Iterator[list[str]]:
            batch: list[str] = []
            for data_id in missing:
                candidate = [*batch, data_id]
                if batch and (
                    len(candidate) > batch_size or url_size(candidate) > _DATA_URL_BYTES_CAP
                ):
                    yield batch
                    batch = []
                batch.append(data_id)
            if batch:
                yield batch

        for batch_ids in batches():
            by_id: dict[str, dict[str, Any]] = {}
            duplicate_ids: set[str] = set()
            batch_error = ""
            try:
                if url_size(batch_ids) > _DATA_URL_BYTES_CAP:
                    raise FetchError(f"Data 查询地址过长: {batch_ids[0]}")
                docs = await self.data_find(
                    source.need_fetcher(), {"_id": {"$in": batch_ids}}, limit=str(batch_size)
                )
                for doc in docs:
                    if not isinstance(doc, dict):
                        continue
                    data_id = doc.get("_id")
                    if not isinstance(data_id, str) or data_id not in missing:
                        continue
                    # 别的批次意外返回的文档也不能写入这一批的缓存。
                    if data_id not in batch_ids:
                        continue
                    if data_id in by_id:
                        duplicate_ids.add(data_id)
                    by_id[data_id] = doc
            except FetchError as exc:
                batch_error = str(exc)

            for data_id in batch_ids:
                doc = by_id.get(data_id)
                error = batch_error
                if not error and data_id in duplicate_ids:
                    error = f"Data 接口返回重复 _id: {data_id}"
                if not error and doc is None:
                    error = f"Data 接口查不到 {data_id}"
                for index in missing[data_id]:
                    result = results[index]
                    ref = result.ref
                    if error:
                        older = source.older(DATA_KIND, ref)
                        if older is None:
                            result.error = error
                        else:
                            source.stale.append(Stale(ref.title, DATA_KIND, error))
                            result.value = dict(older)
                    else:
                        source.cache.put(DATA_KIND, ref.page_id, ref.rev_id, doc)
                        result.value = dict(doc) if doc is not None else None
                    completed()
        return results

    async def data_doc(self, source: WikiSource, ref: PageRef) -> dict[str, Any]:
        """一份 Data 文档。先查缓存；新的一版取不下来、缓存里有旧的就先用旧的（记进 ``stale``）。"""
        cached = source.cached(DATA_KIND, ref)
        if cached is not None:
            return dict(cached)
        try:
            docs = await self.data_find(source.need_fetcher(), {"_id": ref.data_id})
            if not docs:
                raise FetchError(f"Data 接口查不到 {ref.data_id}")
        except FetchError as exc:
            older = source.older(DATA_KIND, ref)
            if older is None:
                raise
            source.stale.append(Stale(ref.title, DATA_KIND, str(exc)))
            return dict(older)
        source.cache.put(DATA_KIND, ref.page_id, ref.rev_id, docs[0])
        return docs[0]

    def data_getter(self, source: WikiSource) -> Callable[[PageRef], Awaitable[dict[str, Any]]]:
        """给 :meth:`WikiSource.fetch_many` 用的取数函数。"""

        async def get(ref: PageRef) -> dict[str, Any]:
            return await self.data_doc(source, ref)

        return get

    def cached_data_doc(self, source: WikiSource, ref: PageRef) -> dict[str, Any] | None:
        """只读缓存取 Data 文档：当前修订号没缓存时，退到这个页面缓存过的最新一版。不发请求。"""
        cached = source.cache.get(DATA_KIND, ref.page_id, ref.rev_id)
        if cached is None:
            cached = source.cache.latest(DATA_KIND, ref.page_id)
        return dict(cached) if cached is not None else None

    def change_classifier(self, ctx: ChangeContext) -> Classifier | None:
        namespace = self.cfg.data_namespace

        def classify(change: Change, plan: Plan) -> Verdict | None:
            if change.namespace != namespace:
                return None
            plan.shared_data_at = max(plan.shared_data_at, change.timestamp)
            return Verdict(LABEL_DATA)

        return classify


def create(setup: SiteSetup) -> HuijiKnowledge:
    raw = setup.raw
    defaults = HuijiSettings()
    return HuijiKnowledge(
        HuijiSettings(
            data_namespace=int(raw.get("data_namespace") or defaults.data_namespace),
            data_path=str(raw.get("data_path") or defaults.data_path),
        )
    )
