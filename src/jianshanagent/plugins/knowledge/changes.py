"""wiki 上最近改了什么，以及这些改动意味着这一轮自动更新要做什么。

读的是 ``list=recentchanges``：一个请求最多 500 条，所以没有变化时一轮只花一个请求
（阴阳师 wiki 平时一周 200 条上下）。按时间从旧到新读，读到哪里记在游标里（最后一条的时间和编号），
下一轮接着读；积压得多（机器停了很久）一轮读不完，剩下的下一轮再读。

改动按命名空间分开看：

- 条目（配置的那个命名空间）改了、建了、删了、移动了：重新列页面，有新修订的会被取下来，
  没了的会从库里去掉。
- 模板、模块改了：用到它们的页面的 ``touched`` 会跟着变，重新列页面时就看得出来（见 sources.py）。
- 别的命名空间：核心不认识，挨个问站点插件（``change_classifier``，后加载的先问——站点插件比
  平台插件更清楚一份文档是什么）。插件认得的，在 :class:`Plan` 上记下要做什么：

  - 要重跑管线（它自己入库的内容有新修订）；
  - 某个页面要按源码现渲染（页面的内容来自的那份数据改了，页面自己没有新修订）；
  - 公共数据改了（说不清哪些页面用了它）：记下改动的时间，由自动更新过后把渲染的页面全部重取；
  - 要对一次图库。

  阴阳师 wiki 的具体规则见站点插件 ``huiji_yys`` 的 ``knowledge/changes.py``。
- 没有插件认的（用户、项目、界面文字……）不影响知识库，不管。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field

from jianshanagent.plugins.knowledge.fetcher import DiskCache, WikiFetcher
from jianshanagent.plugins.knowledge.settings import Settings

#: 状态里最多留几个改动过的标题当例子
_SAMPLE_TITLES = 12


@dataclass(frozen=True, slots=True)
class Change:
    rcid: int
    #: 接口的写法：``2026-11-11T03:36:12Z``（这种写法按字符串比大小就是按时间比）
    timestamp: str
    namespace: int
    #: 带命名空间前缀的完整标题，接口给的原样（下划线是空格）
    title: str
    #: edit / new / log
    kind: str
    #: 日志类的是哪一种：upload / delete / move ……
    log: str = ""

    @property
    def name(self) -> str:
        """不带命名空间前缀、空格换回下划线的名字：和 Data 文档的 ``_id``、文件名是同一种写法。"""
        name = self.title.partition(":")[2] if self.namespace else self.title
        return name.replace(" ", "_")


@dataclass(frozen=True, slots=True)
class Cursor:
    """最近更改读到哪里了：最后一条的时间和编号。"""

    timestamp: str = ""
    rcid: int = 0


@dataclass(slots=True)
class ChangeFeed:
    changes: list[Change]
    cursor: Cursor
    #: 还有没读完的，下一轮接着读
    more: bool = False


async def read_changes(
    fetcher: WikiFetcher, cursor: Cursor, *, since: str, max_requests: int = 4
) -> ChangeFeed:
    """``cursor`` 之后的变更，从旧到新。游标是空的（第一次跑）就从 ``since`` 这个时间读起。"""
    params = {
        "action": "query",
        "list": "recentchanges",
        "rcprop": "title|ids|timestamp|loginfo",
        "rctype": "edit|new|log",
        "rcdir": "newer",
        "rcstart": cursor.timestamp or since,
        "rclimit": "500",
    }
    seen = (cursor.timestamp, cursor.rcid)
    changes: list[Change] = []
    cont: dict[str, str] = {}
    more = True
    for _ in range(max_requests):
        data = await fetcher.api("changes", {**params, **cont})
        for item in (data.get("query") or {}).get("recentchanges") or []:
            change = Change(
                rcid=int(item.get("rcid") or 0),
                timestamp=str(item.get("timestamp") or ""),
                namespace=int(item.get("ns") or 0),
                title=str(item.get("title") or ""),
                kind=str(item.get("type") or ""),
                log=str(item.get("logtype") or ""),
            )
            # 起点那一秒的变更接口会再给一遍：上一轮读到过的不要
            if (change.timestamp, change.rcid) > seen:
                changes.append(change)
        if "continue" not in data:
            more = False
            break
        cont = {k: str(v) for k, v in data["continue"].items()}
    last = max(changes, key=lambda c: (c.timestamp, c.rcid), default=None)
    return ChangeFeed(changes, Cursor(last.timestamp, last.rcid) if last else cursor, more)


@dataclass(slots=True)
class Plan:
    """一批变更意味着这一轮要做什么。"""

    #: 要重新列页面、跑一遍知识库的管线
    kb: bool = False
    #: 要对一次图库
    images: bool = False
    #: 内容来自的数据改了、要按源码现渲染的页面
    fresh_titles: set[str] = field(default_factory=set)
    #: 公共数据最近一次改动的时间（接口的写法）；这批里没有就是空的
    shared_data_at: str = ""
    #: 各类变更各有几条（日志和状态页用）
    counts: dict[str, int] = field(default_factory=dict)
    #: 改了哪些，举几个例子
    titles: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ChangeContext:
    """归类最近更改时交给站点插件的东西。"""

    settings: Settings
    #: 原始内容缓存：要查上次取到的数据时用。归类不发请求
    cache: DiskCache | None = None
    #: 这台机器上维护着图库（有图片记录文件、有插件管图库）
    images_on: bool = False


LABEL_PAGES = "条目"
LABEL_TEMPLATES = "模板和模块"
LABEL_OTHER_FILES = "别的文件"
LABEL_OTHER = "其它"


def plan_changes(
    changes: Sequence[Change], settings: Settings, ctx: ChangeContext | None = None
) -> Plan:
    """把一批变更归类。"""
    src, sync = settings.sources, settings.sync
    ctx = ctx or ChangeContext(settings)
    plan = Plan()
    counts: Counter[str] = Counter()
    # 后加载的先问：站点插件排在平台插件后面，它更清楚一份文档是什么
    classifiers = [
        classify
        for site in reversed(tuple(settings.sites))
        if (classify := site.change_classifier(ctx)) is not None
    ]

    for change in changes:
        if change.namespace == src.namespace:
            counts[LABEL_PAGES] += 1
            plan.kb = True
        elif change.namespace in sync.template_namespaces:
            counts[LABEL_TEMPLATES] += 1
            plan.kb = True
        else:
            verdict = next(
                (v for classify in classifiers if (v := classify(change, plan)) is not None), None
            )
            if verdict is None:
                is_file = change.namespace == sync.file_namespace
                counts[LABEL_OTHER_FILES if is_file else LABEL_OTHER] += 1
                continue
            counts[verdict.label] += 1
            if not verdict.relevant:
                continue
        if change.title not in plan.titles and len(plan.titles) < _SAMPLE_TITLES:
            plan.titles.append(change.title)
    plan.counts = dict(counts)
    return plan
