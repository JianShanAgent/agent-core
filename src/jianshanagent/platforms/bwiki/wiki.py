"""BWiki 平台管查询的那一摊：语义查询。

BWiki 的站点都装着 Semantic MediaWiki：页面上的模板把字段记成属性（式神页的「稀有度」
「上线日期」），``api.php`` 的 ``action=ask`` 按条件找页面、按属性取值——一个请求拿到一类条目的
结构化数据，不用逐页去读渲染结果。
平台自己不认任何条目，只是把这个接口备好，给这个平台上的站点插件用（:class:`BwikiPlatform`）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from jianshanagent.plugins.wiki_query.client import WikiQueryClient
from jianshanagent.plugins.wiki_query.provider import SiteSetup, WikiProvider

DEFAULT_PAGE_SIZE = 500
DEFAULT_MAX_RESULTS = 5000


@dataclass(frozen=True, slots=True)
class SemanticRow:
    """语义查询的一条结果：一个页面，和要的那几个属性在它上面的值。"""

    #: 页面标题
    title: str
    url: str
    #: 属性名 → 值（一个属性可以有几个值；页面没有这个属性就是空的）
    values: Mapping[str, tuple[str, ...]]

    def first(self, name: str, default: str = "") -> str:
        """``name`` 这个属性的第一个值；没有就是 ``default``。"""
        return next(iter(self.values.get(name, ())), default)


class BwikiPlatform(WikiProvider):
    id = "bwiki"

    def __init__(self, page_size: int = DEFAULT_PAGE_SIZE, max_results: int = DEFAULT_MAX_RESULTS):
        self.page_size = max(1, page_size)
        self.max_results = max(1, max_results)
        self._client: WikiQueryClient | None = None

    def bind(self, client: WikiQueryClient) -> None:
        self._client = client

    @property
    def client(self) -> WikiQueryClient:
        if self._client is None:
            raise RuntimeError("BWiki 平台还没有接到查询客户端上")
        return self._client

    async def ask(self, conditions: str, printouts: Sequence[str] = ()) -> list[SemanticRow]:
        """语义查询：符合 ``conditions`` 的页面，连同 ``printouts`` 这几个属性的值。

        ``conditions`` 是 Semantic MediaWiki 的条件写法：``[[稀有度::SSR]]``，
        ``[[式神名::+]]``（有这个属性的页面）。结果一次取 ``page_size`` 条，没取完接着翻，
        最多 ``max_results`` 条。
        """
        rows: list[SemanticRow] = []
        offset = 0
        while len(rows) < self.max_results:
            limit = min(self.page_size, self.max_results - len(rows))
            parts = [conditions, *(f"?{name}" for name in printouts)]
            parts += [f"limit={limit}", f"offset={offset}"]
            data = await self.client.api({"action": "ask", "query": "|".join(parts)})
            rows += _rows((data.get("query") or {}).get("results"), printouts)
            more = data.get("query-continue-offset")
            if not isinstance(more, int) or more <= offset:
                break
            offset = more
        return rows


def _rows(results: Any, printouts: Sequence[str]) -> list[SemanticRow]:
    """接口返回的 ``results`` → 结果。有结果时它是「标题 → 条目」，没有结果时是空列表。"""
    if not isinstance(results, dict):
        return []
    rows: list[SemanticRow] = []
    for key, item in results.items():
        found = item.get("printouts")
        raw: dict[str, Any] = found if isinstance(found, dict) else {}
        values = {name: tuple(_text(v) for v in raw.get(name) or []) for name in printouts}
        title, url = str(item.get("fulltext") or key), str(item.get("fullurl") or "")
        rows.append(SemanticRow(title, url, values))
    return rows


def _text(value: Any) -> str:
    """一个属性值 → 文字。日期类型的值是 ``2016-09-09``，页面类型的值是标题，数量类型的值带单位，
    其余照原样（文本类型的值里的 wiki 写法也照原样：``[[少羽大天狗]]``）。"""
    if isinstance(value, dict):
        if "fulltext" in value:
            return str(value["fulltext"])
        if "raw" in value:
            return _date(str(value["raw"]))
        if "value" in value:
            return f"{value['value']}{value.get('unit') or ''}"
    return str(value)


def _date(raw: str) -> str:
    """Semantic MediaWiki 的日期写法（``1/2016/9/9``：历法/年/月/日[/时/分/秒]）→ ``2016-09-09``。

    只写到年或月的照样只给到年或月；认不出来的原样返回。
    """
    parts = raw.split("/")[1:4]
    if not parts or not all(part.isdigit() for part in parts):
        return raw
    year, *rest = parts
    return "-".join([year, *(part.zfill(2) for part in rest)])


def create(setup: SiteSetup) -> BwikiPlatform:
    ask = setup.raw.get("ask") or {}
    return BwikiPlatform(
        int(ask.get("page_size") or DEFAULT_PAGE_SIZE),
        int(ask.get("max_results") or DEFAULT_MAX_RESULTS),
    )
