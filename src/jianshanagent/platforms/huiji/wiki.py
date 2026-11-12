"""灰机平台管查询的那一摊：读 Data 命名空间。

灰机的站点把结构化数据存成 ``Data:xxx.json`` 页面，内容不走 ``api.php``，走平台的查询接口
``<站点>/api/rest_v1/namespace/data``（按 MongoDB 过滤条件查，``_id`` 是带 ``Data:`` 前缀的标题）。
平台自己不认任何条目，只是把这个接口备好，给这个平台上的站点插件用（:class:`HuijiData`）。
"""

from __future__ import annotations

import json
from typing import Any

from jianshanagent.plugins.wiki_query.client import WikiQueryClient
from jianshanagent.plugins.wiki_query.provider import SiteSetup, WikiProvider

DEFAULT_DATA_PATH = "/api/rest_v1/namespace/data"


class HuijiData(WikiProvider):
    id = "huiji"

    def __init__(self, data_path: str = DEFAULT_DATA_PATH):
        self.data_path = data_path
        self._client: WikiQueryClient | None = None

    def bind(self, client: WikiQueryClient) -> None:
        self._client = client

    @property
    def client(self) -> WikiQueryClient:
        if self._client is None:
            raise RuntimeError("灰机平台还没有接到查询客户端上")
        return self._client

    @property
    def data_url(self) -> str:
        return f"{self.client.site_base}{self.data_path}"

    async def find(self, filter_: dict[str, Any]) -> list[dict[str, Any]]:
        """按 MongoDB 过滤条件查 Data 命名空间。"""
        data = await self.client.get_json(
            self.data_url, {"filter": json.dumps(filter_, ensure_ascii=False)}
        )
        return list(data.get("_embedded") or [])

    async def doc(self, doc_id: str) -> dict[str, Any] | None:
        """按 ``_id``（``Data:Soul.json``）取一份文档；没有返回 None。"""
        docs = await self.find({"_id": doc_id})
        return docs[0] if docs else None


def create(setup: SiteSetup) -> HuijiData:
    return HuijiData(str(setup.raw.get("data_path") or DEFAULT_DATA_PATH))
