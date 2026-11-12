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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from jianshanagent.plugins.knowledge.changes import Change, ChangeContext, Plan
from jianshanagent.plugins.knowledge.fetcher import FetchError, WikiFetcher
from jianshanagent.plugins.knowledge.settings import WikiSettings
from jianshanagent.plugins.knowledge.site import Classifier, SitePlugin, SiteSetup, Verdict
from jianshanagent.plugins.knowledge.sources import PageRef, Stale, WikiSource

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
        return list(data.get("_embedded") or [])

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
