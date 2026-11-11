"""把查询结果排成发给用户的文字。只做排版，不取数。

这里是普通页面、候选列表这些通用的；站点插件自己的条目（阴阳师的式神、御魂）由插件自己排
（``WikiProvider.format_page``），用得上这里的 :func:`clip`、:func:`bold`、:func:`finish`。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from jianshanagent.plugins.wiki_query.client import PageContent, SearchHit
from jianshanagent.plugins.wiki_query.provider import WikiProvider

_MARKDOWN_HEADING_RE = re.compile(r"^#+\s*(.*?)\s*$", re.MULTILINE)


def clip(text: str, limit: int, *, markdown: bool = False) -> str:
    """超长的截掉、结尾加省略号。``markdown``：截断处落在一段加粗里面时把加粗收住。"""
    if len(text) <= limit:
        return text
    if not markdown:
        return text[: max(limit - 1, 0)] + "…"
    cut = text[: max(limit - 3, 0)].rstrip("*")  # 留出收住加粗的两个字符；切在记号中间的半个去掉
    if cut.count("**") % 2:
        cut += "**"
    return cut + "…"


def knowledge_extract(text: str) -> str:
    """知识库里的页面正文（小标题是 Markdown 的 ``## …``）→ 和 wiki 接口的页面摘要一样的样子：
    小标题写成【…】，空行去掉。"""
    text = _MARKDOWN_HEADING_RE.sub(r"【\1】", text)
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def bold(text: str, markdown: bool) -> str:
    return f"**{text}**" if markdown else text


def finish(
    lines: Sequence[str], url: str, *, max_chars: int, include_link: bool, markdown: bool
) -> str:
    """正文超长时截断正文，链接始终保留在最后一行。"""
    body = "\n".join(line for line in lines if line)
    link = url if include_link and url else ""
    if link and markdown:
        link = f"[查看 wiki 页面]({link})"
    if not link:
        return clip(body, max_chars, markdown=markdown)
    return clip(body, max_chars - len(link) - 1, markdown=markdown) + "\n" + link


def format_wiki_page(
    page: PageContent, *, max_chars: int = 800, include_link: bool = True, markdown: bool = False
) -> str:
    return finish(
        [bold(f"【{page.title}】", markdown), page.extract],
        page.url,
        max_chars=max_chars,
        include_link=include_link,
        markdown=markdown,
    )


def format_page(
    page: PageContent,
    *,
    max_chars: int = 800,
    include_link: bool = True,
    markdown: bool = False,
    providers: Sequence[WikiProvider] = (),
) -> str:
    """站点插件认得的条目由它排，其余的按普通页面排。"""
    for provider in providers:
        text = provider.format_page(
            page, max_chars=max_chars, include_link=include_link, markdown=markdown
        )
        if text is not None:
            return text
    return format_wiki_page(page, max_chars=max_chars, include_link=include_link, markdown=markdown)


def format_disambiguation(
    query: str,
    hits: Sequence[SearchHit],
    *,
    command: str = "查",
    buttons: bool = False,
    kind_labels: Mapping[str, str] | None = None,
) -> str:
    """``kind_labels``：站点插件认得的条目种类 → 给人看的名字，标在候选后面。"""
    how = "点下面的按钮，或发送" if buttons else "请发送"
    lines = [f"「{query}」有多个相关条目，{how} /{command} <完整名称>："]
    for number, hit in enumerate(hits, start=1):
        label = (kind_labels or {}).get(hit.kind)
        lines.append(f"{number}. {hit.title}" + (f"（{label}）" if label else ""))
    return "\n".join(lines)


def format_not_found(query: str) -> str:
    return f"没有找到与「{query}」相关的 wiki 条目。"
