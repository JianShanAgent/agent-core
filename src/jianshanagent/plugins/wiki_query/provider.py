"""查询这一摊开给平台和站点插件的接口：这个站有哪几类自己的条目、别名怎么查、另有什么命令和工具。

通用的查询只懂 MediaWiki 的页面（按标题找、全文搜索、取摘要）。一个站常常另有结构化的数据——
阴阳师 wiki 的式神、御魂存在灰机的 Data 命名空间里，``/查 山风`` 要的是稀有度、技能，不是页面摘要。
这类东西由站点插件接进来（扩展点 ``wiki``）：清单的 ``capabilities`` 里写 ``wiki``，
包里的 ``wiki`` 模块有 ``create(setup)``，返回一个 :class:`WikiProvider`
（规范见 ``core/manifest.py``、``core/sites.py``）。核心带着的平台（``jianshanagent/platforms/``）
也是这么接的：它把平台的接口备好（灰机的 Data 查询、BWiki 的语义查询），给那个平台上的站点插件用。

每个方法都有什么都不做的默认实现，用到哪个覆盖哪个。发请求、放缓存用
:class:`~jianshanagent.plugins.wiki_query.client.WikiQueryClient` 的 ``get_json`` / ``api`` /
``recall`` / ``remember``（:meth:`WikiProvider.bind` 时拿到它），
放行请求头、超时、缓存时间才是同一套。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from jianshanagent.core.sites import SiteError, SiteSetup
from jianshanagent.plugins.wiki_query.client import NameMatch, PageContent, SearchHit

if TYPE_CHECKING:
    from jianshanagent.core.plugin import PluginContext
    from jianshanagent.plugins.wiki_query.client import WikiQueryClient

__all__ = ["ProviderContext", "SiteError", "SiteSetup", "WikiProvider"]


@dataclass(slots=True)
class ProviderContext:
    """:meth:`WikiProvider.register` 拿到的东西：往机器人上加自己的命令和工具时用。"""

    #: wiki_query 插件的注册入口（``add_command``、``add_tool``）
    plugin: PluginContext
    client: WikiQueryClient
    #: 一条回复最多多少字（``plugin.wiki_query`` 的 ``format.max_chars``）
    max_chars: int
    #: wiki 访问不了时回给用户的话
    unavailable: str


class WikiProvider:
    """平台、站点插件接查询的基类。"""

    #: 平台、插件的 id，和清单里的一样
    id: str = ""
    #: 这个插件认得的条目种类：代号 → 给人看的名字（``{"character": "式神"}``）。
    #: 代号会出现在搜索结果的 ``kind`` 里，取内容时凭它找回这个插件
    kinds: Mapping[str, str] = {}
    #: 给对话模型看的工具说明里，这个站的条目能怎么找（一句话，句号结尾）
    search_hint: str = ""
    #: 给对话模型看的工具说明里，这个站的条目取回来有什么（分号结尾）
    get_hint: str = ""

    def bind(self, client: WikiQueryClient) -> None:
        """拿到通用的查询客户端。之后发请求、放缓存都走它。"""

    async def search(self, query: str) -> list[SearchHit]:
        """名字里带 ``query`` 的自己的条目：完全一致的 ``exact`` 为 True。
        各个插件一个都没找到，才会去搜页面。"""
        return []

    async def lookup_name(self, name: str) -> list[NameMatch]:
        """``name`` 就是哪个条目的正式名或登记过的别名（完全一致）。"""
        return []

    async def name_candidates(self, name: str, limit: int) -> list[NameMatch]:
        """名字里包含 ``name`` 的条目，最多 ``limit`` 个，``exact`` 为 False（只能当提示）。"""
        return []

    async def get_page(self, title: str, kind: str | None) -> PageContent | None:
        """取一个条目的内容。``kind`` 是 None 表示不知道是哪一类，自己认得就给，认不得返回 None。"""
        return None

    def format_page(
        self, page: PageContent, *, max_chars: int, include_link: bool, markdown: bool
    ) -> str | None:
        """把自己的条目排成发给用户的文字；不是自己的种类返回 None（按普通页面排）。"""
        return None

    def register(self, ctx: ProviderContext) -> None:
        """加这个站自己的命令和给模型用的工具（阴阳师的 ``/筛选``）。"""
