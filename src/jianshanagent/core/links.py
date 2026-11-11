"""回复里的链接核对：模型会编出看着很像的假链接，所以只留「确实查到过的」。

这是程序核对的，不靠模型：回复里出现、但不在查到过的资料里的链接，带文字的留下文字，
光秃秃的网址整个删掉。
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import unquote

_LINK = re.compile(
    r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)|(https?://[^\s<>\[\]（）()「」，。；\"'{}]+)"
)


def normalize_url(url: str) -> str:
    """比较用：解码、去掉页内锚点和结尾的标点。模型常把链接写成解码后的样子，或带上锚点。"""
    return unquote(url.strip()).split("#", 1)[0].rstrip("/.,;:!?").replace(" ", "_")


def urls_in(text: str) -> set[str]:
    """一段文字里出现的全部链接（已规范化）。"""
    return {normalize_url(m.group(2) or m.group(3)) for m in _LINK.finditer(text)}


def urls_in_result(result: Any) -> set[str]:
    """一次工具结果里所有 ``url`` 字段的链接（不管嵌套多深，已规范化）。"""
    found: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            url = node.get("url")
            if isinstance(url, str) and url.startswith("http"):
                found.add(normalize_url(url))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(result)
    return found


def strip_unknown_links(text: str, known: set[str]) -> str:
    """去掉不在 ``known``（已规范化的链接）里的链接：带文字的留下文字，光秃秃的网址整个删掉。"""

    def fix(match: re.Match[str]) -> str:
        label, url = match.group(1), match.group(2) or match.group(3)
        if normalize_url(url) in known:
            return match.group(0)
        return label or ""

    return _LINK.sub(fix, text)
