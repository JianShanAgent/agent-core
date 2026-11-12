"""读取知识库插件的配置（``plugins/knowledge/config.yaml``），整理成带默认值的设置对象，并加载站点插件。

这里只有对任何 MediaWiki 都成立的设置。平台、站点特有的设置在各个站点插件自己的配置里
（``site.<id>``），由插件自己读（见 site.py）；它们对这份配置的补充读配置时已经并进来了。

部署挂了几个 wiki，知识库就各建各的：一个 wiki 一份设置（它自己的站点插件、它那一份配置），
文件放在知识库目录下它自己的那一层（``<知识库目录>/<wiki 的 id>/``）。
"""

from __future__ import annotations

import dataclasses
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jianshanagent.core.config import AppConfig, load_config
from jianshanagent.core.sites import EXTENSION_KNOWLEDGE, load_sites
from jianshanagent.core.wiki import WikiSite, load_wiki, load_wikis
from jianshanagent.plugins.knowledge.site import SiteSet

#: 对 wiki 服务器的硬上限：配置里写得再大也不会超过（这是别人的生产站点）
MAX_CONCURRENCY_CAP = 2
MAX_RPS_CAP = 2.0
#: 自动更新一轮最多向 wiki 发多少个请求，配置里写得再大也不会超过
MAX_SYNC_REQUESTS_CAP = 2000
#: 自动更新两次检查之间最少隔多少秒，配置里写得再小也不会低于
MIN_SYNC_INTERVAL_SECONDS = 300.0
#: 设了这个环境变量，知识库目录就是它，不看配置里的 ``paths``。
#: 服务器的容器里设成 ``/data/kb``（数据盘）：配置里的相对路径在容器里落在镜像里，不能写
KB_DIR_ENV = "JIANSHANAGENT_KB_DIR"
#: 一个 wiki 的原始内容缓存，在它那一层目录下面叫这个名字
CACHE_DIRNAME = "cache"
#: 取数用的 wiki 设置：地址、放行密钥来自 ``core.wiki.yaml``（和查询插件共用）
WikiSettings = WikiSite


@dataclass(frozen=True, slots=True)
class FetchSettings:
    max_concurrency: int = 2
    requests_per_second: float = 2.0
    timeout_seconds: float = 60.0
    max_retries: int = 4
    wikitext_batch_size: int = 50


@dataclass(frozen=True, slots=True)
class SourceSettings:
    """条目页：列哪个命名空间、哪些不要、怎么认导航页和空页。"""

    namespace: int = 0
    exclude_titles: frozenset[str] = frozenset()
    nav_link_ratio: float = 0.6
    nav_max_plain_chars: int = 100
    #: 导航页里「有名字的链接」至少这么多个、占站内链接的比例不低于这个值，才算章节目录页（入库）
    directory_min_links: int = 3
    directory_min_share: float = 0.5
    min_chars: int = 20
    broken_markers: tuple[str, ...] = ()
    broken_max_chars: int = 200


#: 任何 MediaWiki 的渲染结果里都该去掉的界面元素：导航框、目录、编辑链接、参考资料的回跳、
#: 空壳、不参与打印的元素、缩略图的放大图标。配置里写的是在这之上再加的
GENERIC_REMOVE_CLASSES = frozenset(
    {
        "navbox",
        "vertical-navbox",
        "toc",
        "mw-editsection",
        "mw-cite-backlink",
        "mw-empty-elt",
        "noprint",
        "magnify",
    }
)


@dataclass(frozen=True, slots=True)
class CleanSettings:
    #: 按 class 整块移除。默认值是 MediaWiki 通用的那几样（:data:`GENERIC_REMOVE_CLASSES`）
    remove_classes: frozenset[str] = GENERIC_REMOVE_CLASSES
    #: class → 相对层级（1 表示所在章节的下一级）
    heading_classes: dict[str, int] = field(default_factory=dict)
    term_classes: frozenset[str] = frozenset()
    inline_classes: frozenset[str] = frozenset()
    prefix_classes: dict[str, str] = field(default_factory=dict)
    #: class → 要补在这个区块前面的标题文字
    section_titles: dict[str, str] = field(default_factory=dict)
    #: 卡片 class → 卡片头部的 class（头部链接指向已有条目的卡片整张移除）
    linked_cards: dict[str, str] = field(default_factory=dict)
    #: 只有图标的徽章：class → 文字（接到它后面那个兄弟元素的文字末尾）
    badge_texts: dict[str, str] = field(default_factory=dict)
    #: 表格里表示「没有」的占位值；一行除第一格外全是占位值就不要这一行
    table_placeholders: frozenset[str] = frozenset()
    tabber_class: str = "tabber"
    tabber_item_class: str = "tabber-item"
    tabber_label_class: str = "tabber-filter-item"
    #: 标签页的名字写在内容块的哪个属性上（MediaWiki 的 Tabber 扩展是 ``title``）；空 = 只认按钮
    tabber_label_attribute: str = ""
    nav_block_min_links: int = 10
    nav_block_link_ratio: float = 0.8
    nav_block_max_share: float = 0.3


@dataclass(frozen=True, slots=True)
class DateSettings:
    """条目日期的识别与推断（规则见 dates.py 顶部）。"""

    #: 日期紧跟在这些字样后面，才算页面自己写明的活动日期
    time_keywords: tuple[str, ...] = (
        "活动时间",
        "活动期间",
        "活动日期",
        "开启时间",
        "开放时间",
        "持续时间",
        "举办时间",
    )
    keyword_window: int = 40
    lead_chars: int = 300
    #: 早于这一年的年份不可能是这个站讲的活动的日期，当作正文顺带提到的历史年份
    min_year: int = 2000
    #: 页面写明的几个活动日期相差不超过这么多天，才算同一期活动
    same_event_days: int = 92
    #: 月日前后出现这些字样的，本来就不该带年份（生日、每年固定的日子）
    non_event_markers: tuple[str, ...] = ("生日", "诞辰", "诞生日", "生辰", "每年", "农历")
    marker_window: int = 12
    infer_enabled: bool = True
    #: 这些前缀的页面不推断（内容天天在变的页面，比如首页的组件）
    exclude_prefixes: tuple[str, ...] = ()
    min_name_chars: int = 4
    match_window_days: int = 31
    event_span_days: int = 62
    max_maintained_days: int = 180


@dataclass(frozen=True, slots=True)
class ChunkSettings:
    target_chars: int = 800
    max_chars: int = 1200
    min_chars: int = 120
    label_max_chars: int = 40


@dataclass(frozen=True, slots=True)
class ReportSettings:
    sample_size: int = 20
    sample_seed: int = 20261111
    entity_sample_size: int = 10
    acceptance_page: str = ""
    acceptance_must_contain: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SyncSettings:
    """自动更新（做法见 sync.py 顶部）。"""

    enabled: bool = True
    #: 每隔多少秒看一次 wiki 的最近更改
    interval_seconds: float = 3600.0
    #: 模板、模块所在的命名空间：它们改了要重新列一次页面，看哪些页面的渲染结果跟着失效了
    template_namespaces: tuple[int, ...] = (10, 828)
    #: 文件所在的命名空间
    file_namespace: int = 6
    #: 第一次跑、或者停了很久以后，最多往回看多少天的最近更改
    lookback_days: float = 30.0
    #: 读最近更改一轮最多翻几页（一页 500 条）；没读完的下一轮接着读
    changes_max_requests: int = 4
    #: 不管有没有变化，至少每隔这么多天把渲染的页面全部重取一遍
    full_every_days: float = 7.0
    #: 说不清被哪些页面用到的公共数据改了以后（由站点插件认，见 changes.py），也全部重取一遍
    full_on_shared_data: bool = True
    #: 上一条要等改动过去这么多小时再做：wiki 自己的渲染缓存要这么久才换成新的
    full_settle_hours: float = 24.0
    #: 全部重取只在这个时段做：（开始，结束），从零点起的分钟数；None = 不限时段
    full_window: tuple[int, int] | None = (180, 360)
    #: 两次全部重取之间至少隔这么多小时
    full_min_interval_hours: float = 20.0
    #: 没有相关变更时，图库的文件清单至少每隔这么多小时对一次
    images_every_hours: float = 24.0
    #: 一轮最多向 wiki 发多少个请求。超了以后还没取的页面沿用旧内容，下一轮接着取
    max_requests: int = 600
    #: 下面三项是闸门：超过了多半是出了岔子（文件没拷全、wiki 返回的清单不全），
    #: 这一轮停在那一步，不往下做，等人来看
    #: 要新算向量的切片多于这么多片
    max_embed_chunks: int = 3000
    #: 要新下载、新算向量的图片多于这么多张
    max_new_images: int = 300
    #: 文档数、切片数或图片数比上一次少了超过这个比例
    max_drop_ratio: float = 0.05
    #: 取数失败、又没有旧内容可用的文档多于这么多篇
    max_failures: int = 20

    def in_full_window(self, minute_of_day: int) -> bool:
        if self.full_window is None:
            return True
        start, end = self.full_window
        if start <= end:
            return start <= minute_of_day < end
        return minute_of_day >= start or minute_of_day < end  # 跨零点的时段


@dataclass(frozen=True, slots=True)
class Settings:
    wiki: WikiSettings
    fetch: FetchSettings
    sources: SourceSettings
    clean: CleanSettings
    chunk: ChunkSettings
    report: ReportSettings
    dates: DateSettings
    #: 这个 wiki 的切片、向量、报告放在哪：``<知识库目录>/<wiki 的 id>/``
    output_dir: Path
    #: 这个 wiki 的原始内容缓存、自动更新的游标：``<输出目录>/cache/``
    cache_dir: Path
    sync: SyncSettings = field(default_factory=SyncSettings)
    #: 加载好的站点插件（见 site.py）；一个都没有就是纯 MediaWiki
    sites: SiteSet = field(default_factory=SiteSet)

    @property
    def vectors_dir(self) -> Path:
        """向量文件的目录，里面按嵌入提供方再分一层（见 embed.py）。"""
        return self.output_dir / "vectors"


def _strs(value: Any) -> list[str]:
    return [str(v) for v in value or [] if str(v)]


def parse_window(text: str) -> tuple[int, int] | None:
    """``"03:00-06:00"`` →（180, 360）。空的 = 不限时段；写得不对就报错，不悄悄当成不限。"""
    text = str(text or "").strip()
    if not text:
        return None
    try:
        start, end = (part.strip().split(":") for part in text.split("-"))
        minutes = (int(start[0]) * 60 + int(start[1]), int(end[0]) * 60 + int(end[1]))
    except (ValueError, IndexError):
        raise ValueError(f"时段要写成 03:00-06:00 这样，现在是「{text}」") from None
    if not all(0 <= minute <= 24 * 60 for minute in minutes) or minutes[0] == minutes[1]:
        raise ValueError(f"时段要写成 03:00-06:00 这样，现在是「{text}」")
    return minutes


def _sync_settings(raw: dict[str, Any]) -> SyncSettings:
    defaults = SyncSettings()
    full = raw.get("full_refresh") or {}
    limits = raw.get("limits") or {}

    def number(section: dict[str, Any], key: str, default: float) -> float:
        # 0 是有意义的值（比如「不用等」），所以只在没写时用默认值
        return float(section[key]) if section.get(key) is not None else default

    interval = number(raw, "interval_minutes", defaults.interval_seconds / 60) * 60
    return SyncSettings(
        enabled=bool(raw.get("enabled", defaults.enabled)),
        interval_seconds=max(MIN_SYNC_INTERVAL_SECONDS, interval),
        template_namespaces=tuple(int(n) for n in raw.get("template_namespaces") or ())
        or defaults.template_namespaces,
        file_namespace=int(number(raw, "file_namespace", defaults.file_namespace)),
        lookback_days=number(raw, "lookback_days", defaults.lookback_days),
        changes_max_requests=max(
            1, int(number(raw, "changes_max_requests", defaults.changes_max_requests))
        ),
        full_every_days=number(full, "every_days", defaults.full_every_days),
        full_on_shared_data=bool(full.get("on_shared_data", defaults.full_on_shared_data)),
        full_settle_hours=number(full, "settle_hours", defaults.full_settle_hours),
        full_window=parse_window(full["window"]) if "window" in full else defaults.full_window,
        full_min_interval_hours=number(
            full, "min_interval_hours", defaults.full_min_interval_hours
        ),
        images_every_hours=number(raw, "images_every_hours", defaults.images_every_hours),
        max_requests=max(
            1, min(MAX_SYNC_REQUESTS_CAP, int(number(limits, "requests", defaults.max_requests)))
        ),
        max_embed_chunks=int(number(limits, "embed_chunks", defaults.max_embed_chunks)),
        max_new_images=int(number(limits, "new_images", defaults.max_new_images)),
        max_drop_ratio=number(limits, "drop_ratio", defaults.max_drop_ratio),
        max_failures=int(number(limits, "failures", defaults.max_failures)),
    )


def clean_settings(c: dict[str, Any]) -> CleanSettings:
    """``clean`` 一段 → 清洗规则。"""
    defaults = CleanSettings()
    return CleanSettings(
        remove_classes=defaults.remove_classes | frozenset(_strs(c.get("remove_classes"))),
        heading_classes={str(k): int(v) for k, v in (c.get("heading_classes") or {}).items()},
        term_classes=frozenset(_strs(c.get("term_classes"))),
        inline_classes=frozenset(_strs(c.get("inline_classes"))),
        prefix_classes={str(k): str(v) for k, v in (c.get("prefix_classes") or {}).items()},
        section_titles={str(k): str(v) for k, v in (c.get("section_titles") or {}).items()},
        linked_cards={str(k): str(v) for k, v in (c.get("linked_cards") or {}).items()},
        badge_texts={str(k): str(v) for k, v in (c.get("badge_texts") or {}).items()},
        table_placeholders=frozenset(_strs(c.get("table_placeholders"))),
        tabber_class=str(c.get("tabber_class") or defaults.tabber_class),
        tabber_item_class=str(c.get("tabber_item_class") or defaults.tabber_item_class),
        tabber_label_class=str(c.get("tabber_label_class") or defaults.tabber_label_class),
        tabber_label_attribute=str(
            c.get("tabber_label_attribute") or defaults.tabber_label_attribute
        ),
        nav_block_min_links=int(c.get("nav_block_min_links") or defaults.nav_block_min_links),
        nav_block_link_ratio=float(c.get("nav_block_link_ratio") or defaults.nav_block_link_ratio),
        nav_block_max_share=float(c.get("nav_block_max_share") or defaults.nav_block_max_share),
    )


def _wiki_settings(config: AppConfig, wiki_id: str, raw: dict[str, Any]) -> WikiSettings:
    """``core.wiki.yaml`` 里的那个 wiki；``plugin.knowledge`` 另写了 User-Agent 就用它的。"""
    wiki = load_wiki(config, wiki_id)
    user_agent = str((raw.get("wiki") or {}).get("user_agent") or "")
    return dataclasses.replace(wiki, user_agent=user_agent) if user_agent else wiki


def kb_root(config: AppConfig) -> Path:
    """知识库目录：各个 wiki 的文件都在它下面，一个 wiki 一层。"""
    paths = config.plugin("knowledge").get("paths") or {}
    old = [key for key in ("output_dir", "cache_dir") if key in paths]
    if old:
        raise ValueError(
            f"plugin.knowledge 的 paths 里的 {'、'.join(old)} 不用了：改成 paths.root"
            "（知识库目录），每个 wiki 的文件在它下面的 <wiki 的 id>/，缓存在 <wiki 的 id>/cache/"
        )
    kb_dir = os.environ.get(KB_DIR_ENV, "").strip()
    if kb_dir:
        return Path(kb_dir).resolve()
    return (config.root.parent / str(paths.get("root") or "data/kb")).resolve()


def is_primary(config: AppConfig, settings: Settings) -> bool:
    """这份设置是不是主 wiki（``core.wiki.yaml`` 里排第一的那个）的。

    库里没标出处的旧内容算主 wiki 的：导入主 wiki 时把它们一起换掉。
    """
    return not config.wikis or config.wikis[0].id == settings.wiki.id


def load_settings(config: AppConfig | None = None, wiki: str = "") -> Settings:
    """``wiki`` 这个 wiki（不写就是主 wiki）的知识库设置：读它那一份配置
    （它的站点插件的补充读配置时已经并进来了），再把它的站点插件装起来。"""
    config = config or load_config()
    wiki_id = wiki or load_wikis(config)[0].id  # 一个 wiki 都没挂，在这里报错
    root = kb_root(config)
    config = config.for_wiki(wiki_id)
    raw = config.plugin("knowledge")
    site = _wiki_settings(config, wiki_id, raw)

    f = raw.get("fetch") or {}
    fetch = FetchSettings(
        max_concurrency=max(1, min(MAX_CONCURRENCY_CAP, int(f.get("max_concurrency") or 2))),
        requests_per_second=max(0.1, min(MAX_RPS_CAP, float(f.get("requests_per_second") or 2))),
        timeout_seconds=float(f.get("timeout_seconds") or 60),
        max_retries=int(f.get("max_retries") or 4),
        wikitext_batch_size=max(1, min(50, int(f.get("wikitext_batch_size") or 50))),
    )

    pages = (raw.get("sources") or {}).get("pages") or {}
    directory = pages.get("directory") or {}
    defaults = SourceSettings()
    sources = SourceSettings(
        namespace=int(pages.get("namespace") or 0),
        exclude_titles=frozenset(_strs(pages.get("exclude_titles"))),
        nav_link_ratio=float(pages.get("nav_link_ratio") or defaults.nav_link_ratio),
        nav_max_plain_chars=int(pages.get("nav_max_plain_chars") or defaults.nav_max_plain_chars),
        directory_min_links=int(directory.get("min_links") or defaults.directory_min_links),
        directory_min_share=float(directory.get("min_share") or defaults.directory_min_share),
        min_chars=int(pages.get("min_chars") or defaults.min_chars),
        broken_markers=tuple(_strs(pages.get("broken_markers"))),
        broken_max_chars=int(pages.get("broken_max_chars") or defaults.broken_max_chars),
    )

    k = raw.get("chunk") or {}
    chunk = ChunkSettings(
        target_chars=int(k.get("target_chars") or 800),
        max_chars=int(k.get("max_chars") or 1200),
        min_chars=int(k.get("min_chars") or 120),
        label_max_chars=int(k.get("label_max_chars") or 40),
    )
    if not 0 < chunk.min_chars <= chunk.target_chars <= chunk.max_chars:
        raise ValueError("chunk 配置需满足 0 < min_chars <= target_chars <= max_chars")

    r = raw.get("report") or {}
    acceptance = r.get("acceptance") or {}
    report = ReportSettings(
        sample_size=int(r.get("sample_size") or 20),
        sample_seed=int(r.get("sample_seed") or 20261111),
        entity_sample_size=int(r.get("entity_sample_size") or 10),
        acceptance_page=str(acceptance.get("page") or ""),
        acceptance_must_contain=tuple(_strs(acceptance.get("must_contain"))),
    )

    d = raw.get("dates") or {}
    infer = d.get("infer") or {}
    date_defaults = DateSettings()
    dates = DateSettings(
        time_keywords=tuple(_strs(d.get("time_keywords"))) or date_defaults.time_keywords,
        keyword_window=int(d.get("keyword_window") or date_defaults.keyword_window),
        lead_chars=int(d.get("lead_chars") or date_defaults.lead_chars),
        min_year=int(d.get("min_year") or date_defaults.min_year),
        same_event_days=int(d.get("same_event_days") or date_defaults.same_event_days),
        non_event_markers=tuple(_strs(d.get("non_event_markers")))
        or date_defaults.non_event_markers,
        marker_window=int(d.get("marker_window") or date_defaults.marker_window),
        infer_enabled=bool(infer.get("enabled", True)),
        exclude_prefixes=tuple(_strs(infer.get("exclude_prefixes"))),
        min_name_chars=int(infer.get("min_name_chars") or date_defaults.min_name_chars),
        match_window_days=int(infer.get("match_window_days") or date_defaults.match_window_days),
        event_span_days=int(infer.get("event_span_days") or date_defaults.event_span_days),
        max_maintained_days=int(
            infer.get("max_maintained_days") or date_defaults.max_maintained_days
        ),
    )

    output_dir = root / site.id

    # 这个 wiki 用到的各级站点插件里接了知识库这个扩展点的，从上到下
    sites = SiteSet(load_sites(config, EXTENSION_KNOWLEDGE, site))
    return Settings(
        wiki=site,
        fetch=fetch,
        sources=sources,
        clean=clean_settings(raw.get("clean") or {}),
        chunk=chunk,
        report=report,
        dates=dates,
        sync=_sync_settings(raw.get("sync") or {}),
        output_dir=output_dir,
        cache_dir=output_dir / CACHE_DIRNAME,
        sites=sites,
    )
