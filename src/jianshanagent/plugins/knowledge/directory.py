"""章节目录页：整页几乎只有链接，但链接本身就是内容（主线各章、某次活动的各段剧情、活动一览）。

「纯导航页」原先一律不入库。其中有两类，靠页面内容本身区分，不靠页面名单：

- 章节目录页：链接的文字是名字（「第一章 雀食奇谭」「砂城之章 洪狐天运」），
  链接指向本页的子页面、剧情页面或别的已有条目。这类入库。
- 翻页导航：链接的文字只是编号（花合战漫画月份页上的「01 02 03 …」），正文只有图片。
  这类仍然不入库。

判定：站内链接里「文字是名字、而且指向已有页面」的不少于 ``directory_min_links`` 个，
并且占全部站内链接的比例不低于 ``directory_min_share``。

入库时把目录改写成带上下文的文字：每组链接前加一句「『页面名』某节 的目录（共 N 项）：」，
链接各占一行。小节层级（活动 → 剧情名 → 章节）由原页面的标题带着，切片首行能看到完整路径。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import unquote

from lxml.html import HtmlElement

from jianshanagent.plugins.knowledge.models import BLOCK_HEADING, BLOCK_LIST, BLOCK_PARA, Block

_WIKI_PREFIX = "/wiki/"
#: 名字：至少连着两个汉字 / 假名 / 字母（「01」「2020」这种编号不算）
_NAME_RE = re.compile(r"[A-Za-z\u3400-\u9fff\u3040-\u30ff]{2,}")
_OTHER_NAMESPACE_RE = re.compile(
    r"^(文件|File|分类|Category|特殊|Special|模板|Template|模块|Module|Data)\s*[:：]",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class DirectoryLink:
    text: str  # 链接文字，各段之间一个空格（「第一章 狩日再临」）
    target: str  # 链接指向的页面标题


@dataclass(slots=True)
class DirectoryInfo:
    """一个页面里站内链接的情况。"""

    internal: int = 0  # 指向条目的站内链接数
    links: list[DirectoryLink] | None = None  # 其中文字是名字、指向已有页面的
    to_subpages: int = 0  # 指向本页的子页面
    to_story_pages: int = 0  # 指向剧情页面（可以同时是本页的子页面）
    to_other_pages: int = 0  # 指向别的已有条目

    @property
    def named(self) -> int:
        return len(self.links or [])

    def is_directory(self, min_links: int, min_share: float) -> bool:
        return self.named >= min_links and self.named / max(1, self.internal) >= min_share


def _key(text: str) -> str:
    return re.sub(r"\s+", "", text)


def analyse_links(
    root: HtmlElement,
    title: str,
    *,
    known_titles: frozenset[str] | set[str] | None = None,
    story_titles: frozenset[str] | set[str] = frozenset(),
    link_prefix: str = _WIKI_PREFIX,
) -> DirectoryInfo:
    """数清洗后的页面里的站内链接。``known_titles`` 为 None 时不核对链接目标是否存在。

    ``link_prefix``：指向站内页面的链接以什么开头（:attr:`WikiSettings.link_prefix`）。"""
    info = DirectoryInfo(links=[])
    for link in root.iter("a"):
        href = link.get("href") or ""
        if not href.startswith(link_prefix):
            continue  # 红链（/index.php?…redlink=1）和站外链接
        target = unquote(href[len(link_prefix) :]).split("#", 1)[0].replace("_", " ").strip()
        if not target or _OTHER_NAMESPACE_RE.match(target):
            continue
        info.internal += 1
        text = " ".join(part.strip() for part in link.itertext() if part.strip())
        if not _NAME_RE.search(_key(text)):
            continue
        subpage = target.startswith(f"{title}/")
        story = target in story_titles
        if not (subpage or story or known_titles is None or target in known_titles):
            continue
        info.to_subpages += subpage
        info.to_story_pages += story
        info.to_other_pages += not (subpage or story)
        info.links.append(DirectoryLink(text, target))
    return info


def directory_blocks(blocks: list[Block], links: list[DirectoryLink], title: str) -> list[Block]:
    """把目录页的块改写成带上下文的文字。不是链接的块（标题、零散说明）原样保留。"""
    by_text = {_key(link.text): link for link in links}
    out: list[Block] = []
    path: list[tuple[int, str]] = []
    pending: list[DirectoryLink] = []

    def flush() -> None:
        if not pending:
            return
        where = " > ".join(text for _, text in path)
        lead = f"「{title}」{where} 的目录" if where else f"「{title}」的目录"
        out.append(Block(BLOCK_PARA, text=f"{lead}（共 {len(pending)} 项）："))
        out.append(Block(BLOCK_LIST, items=[f"- {_item(link)}" for link in pending]))
        pending.clear()

    for block in blocks:
        link = by_text.get(_key(block.text)) if block.kind == BLOCK_PARA else None
        if link is not None:
            pending.append(link)
            continue
        flush()
        if block.kind == BLOCK_HEADING:
            while path and path[-1][0] >= block.level:
                path.pop()
            path.append((block.level, block.text))
        out.append(block)
    flush()
    return out


def _item(link: DirectoryLink) -> str:
    """目录里的一行。链接文字里看不出页面名时（文字是别称）把页面名补在后面。"""
    name = link.target.rsplit("/", 1)[-1]
    if _key(name) in _key(link.text):
        return link.text
    return f"{link.text}（页面：{link.target}）"
