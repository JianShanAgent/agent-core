"""知识库开给平台和站点插件的扩展点（``knowledge``）。

分三层：

- 通用的（这个包里的模块）：对任何 MediaWiki 都成立的做法——列页面、取渲染结果、
  清洗、转 Markdown、切片、去重、算向量、入库、检索，以及按最近更改做的自动更新。
- 平台（``huiji``，核心带着的，在 ``jianshanagent/platforms/``）：一个 wiki 平台特有的东西。
  灰机有 Data 命名空间和查询接口。
- 站点插件（``huiji_yys``，灰机上的一个站）：一个站特有的东西。阴阳师 wiki 的公告、剧情、数据集、
  实体字典、图库。

平台和站点插件接进来的方式是同一个，见 ``core/manifest.py`` 和 ``core/sites.py``：清单的
``capabilities`` 里写 ``knowledge``，包里的 ``knowledge`` 模块有 ``create(setup)``，
返回一个 :class:`SitePlugin`。清洗规则这类「对通用配置的补充」不经过这里：写在自己配置的
``extend`` 段里，读配置时就并进 ``plugin.knowledge`` 了。

能插手的地方就是 :class:`SitePlugin` 的那些方法，都有什么都不做的默认实现，用到哪个覆盖哪个。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from jianshanagent.core.sites import SiteError, SiteSetup

if TYPE_CHECKING:
    from jianshanagent.plugins.knowledge.changes import Change, ChangeContext, Plan
    from jianshanagent.plugins.knowledge.collect import CollectContext, RawCorpus
    from jianshanagent.plugins.knowledge.image_collect import Catalog
    from jianshanagent.plugins.knowledge.pipeline import BuildContext, ProcessContext, ProcessResult
    from jianshanagent.plugins.knowledge.report import ReportWriter
    from jianshanagent.plugins.knowledge.settings import Settings
    from jianshanagent.plugins.knowledge.sources import ListingSpec, WikiSource

__all__ = ["Classifier", "SiteError", "SitePlugin", "SiteSet", "SiteSetup", "Verdict"]


@dataclass(frozen=True, slots=True)
class Verdict:
    """插件对一条最近更改的判断。"""

    #: 这条改动算哪一类（日志和管理后台里按它计数）
    label: str
    #: 和知识库有没有关系。没关系的只计数，不举例子
    relevant: bool = True


#: 看一条最近更改：归自己管就在 ``plan`` 上记下要做什么并返回判断，不归自己管返回 None
Classifier = Callable[["Change", "Plan"], Verdict | None]


class SitePlugin:
    """平台、站点插件接知识库的基类。每个方法都是一个可以插手的地方，默认什么都不做。"""

    #: 平台、插件的 id，和清单里的一样
    id: str = ""

    # ── 取数 ──────────────────────────────────────────────────────────

    def listing(self) -> ListingSpec | None:
        """除了条目页以外，列页面清单时还要列哪些（按前缀列的、按标题点名的）。"""
        return None

    async def select_pages(self, ctx: CollectContext) -> None:
        """渲染页面之前：哪些页面不渲染、由插件自己的文档代替（``ctx.replace``）。"""

    async def collect(self, ctx: CollectContext) -> None:
        """页面渲染完以后：取插件自己要的原始内容，放进 ``ctx.corpus.extras``。"""

    def summary(self, corpus: RawCorpus) -> dict[str, Any]:
        """取到了什么（日志和统计里用）。"""
        return {}

    # ── 建文档、切片 ──────────────────────────────────────────────────

    def build_documents(self, ctx: BuildContext) -> None:
        """条目页的文档建好以后：把插件自己取的原始内容变成文档，加进 ``ctx.built``。"""

    def annotate(self, ctx: ProcessContext) -> None:
        """切片、去重以后：给切片补东西（比如标实体）。"""

    def stats(self, ctx: ProcessContext) -> dict[str, Any]:
        """插件自己的统计，并进这次运行的统计里。"""
        return {}

    # ── 报告 ──────────────────────────────────────────────────────────

    def status_labels(self) -> dict[str, str]:
        """插件自己定的「没有入库的原因」→ 报告里的说法。"""
        return {}

    def report(
        self, slot: str, out: ReportWriter, result: ProcessResult, settings: Settings
    ) -> None:
        """往质量报告里加内容。``slot`` 是报告里留给插件的位置（见 report.py 的 ``SLOT_*``）。"""

    # ── 自动更新 ──────────────────────────────────────────────────────

    def change_classifier(self, ctx: ChangeContext) -> Classifier | None:
        """条目、模板以外的最近更改归不归自己管、意味着要做什么（见 changes.py）。"""
        return None

    # ── 图库 ──────────────────────────────────────────────────────────

    def has_images(self) -> bool:
        """这个插件管不管图库（以图搜图用的图片索引）。"""
        return False

    async def image_catalog(self, source: WikiSource, settings: Settings) -> Catalog | None:
        """选图、生成图片记录。不管图库的返回 None。"""
        return None


class SiteSet:
    """给一个 wiki 加载好的平台和站点插件，按加载顺序（平台在前）。"""

    def __init__(self, plugins: Iterable[SitePlugin] = ()):
        self._plugins: tuple[SitePlugin, ...] = ()
        for plugin in plugins:
            self._add(plugin)

    def _add(self, plugin: SitePlugin) -> None:
        if not plugin.id:
            raise SiteError(f"站点插件 {type(plugin).__name__} 没有写 id")
        if self.get(plugin.id) is not None:
            raise SiteError(f"站点插件「{plugin.id}」加载了两次")
        self._plugins = (*self._plugins, plugin)

    def __iter__(self) -> Iterator[SitePlugin]:
        return iter(self._plugins)

    def __len__(self) -> int:
        return len(self._plugins)

    def __repr__(self) -> str:
        return f"SiteSet({[plugin.id for plugin in self._plugins]})"

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(plugin.id for plugin in self._plugins)

    def get(self, plugin_id: str) -> SitePlugin | None:
        return next((plugin for plugin in self._plugins if plugin.id == plugin_id), None)

    def require(self, plugin_id: str) -> SitePlugin:
        plugin = self.get(plugin_id)
        if plugin is None:
            raise SiteError(f"没有加载站点插件「{plugin_id}」")
        return plugin

    def with_plugin(self, plugin: SitePlugin) -> SiteSet:
        return SiteSet((*self._plugins, plugin))

    @property
    def has_images(self) -> bool:
        return any(plugin.has_images() for plugin in self._plugins)
