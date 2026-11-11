"""把一份 YAML 配置变成管理后台里的表单：有哪些项、各是什么类型、每一项的说明。

说明来自配置文件里的注释——这个项目的配置注释写得很细（为什么是这个值、实测结果），
表单直接拿来当帮助文字，不另外维护一份。注释和配置项的对应关系按书写习惯认：

- 文件最上面的那一段注释是整个文件的说明。
- 紧挨在一项上面的注释（中间没有空行）是这一项的说明。
- 一项后面同一行的注释也是它的说明；下一行如果只有注释、而且和上一行的注释对齐
  （不是顶着配置项的缩进写的），算作它的续行。
- 列表里面的项（``- xxx`` 下面的键）不单独给说明，整个列表当一项。

类型从默认值看：真假、整数、小数、文字（长的或多行的用多行框）、只含单个值的列表；
别的（列表里套着结构的）在表单里按 JSON 整个编辑。

不许在网页上改的项（接口地址、用哪个密钥……见 core/config.py 的 ``LOCKED_KEYS``）带一个
``locked`` 标记，页面把它们显示成只读——改了也存不上，不让人白改一通。
"""

from __future__ import annotations

import re
from typing import Any

from jianshanagent.core.config import locked_changes

_BLOCK_SCALAR = re.compile(r"^[|>][+-]?\d*$")
_LONG_TEXT = 80
#: 续行注释的 # 最多可以比上一行注释的 # 靠左这么多（中文是全角的，按字符数算的列对不齐）
_ALIGN_SLACK = 2


def _split_comment(text: str) -> tuple[str, str, int]:
    """把一段（键后面的）内容拆成（值，注释，注释的 # 在这段里的位置）。没有注释时位置是 -1。

    引号里的 # 不算注释；注释的 # 前面要有空白，或者在最开头。
    """
    quote = ""
    for position, char in enumerate(text):
        if quote:
            if char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
        elif char == "#" and (position == 0 or text[position - 1].isspace()):
            return text[:position].strip(), text[position + 1 :].strip(), position
    return text.strip(), "", -1


def _split_key(line: str) -> tuple[str, str] | None:
    """``键: 其余`` → （键，其余）。不是这种形状的行返回 None。"""
    quote = ""
    for position, char in enumerate(line):
        if quote:
            if char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
        elif char == "#":
            return None
        elif char == ":" and (position + 1 == len(line) or line[position + 1].isspace()):
            key = line[:position].strip()
            if len(key) >= 2 and key[0] == key[-1] and key[0] in "\"'":
                key = key[1:-1]
            return (key, line[position + 1 :]) if key else None
    return None


def read_comments(text: str) -> tuple[str, dict[tuple[str, ...], str]]:
    """YAML 原文 → （整个文件的说明，各项的说明）。各项用从外到里的键名当索引。"""
    header: list[str] | None = None
    notes: dict[tuple[str, ...], list[str]] = {}
    stack: list[tuple[int, str]] = []
    pending: list[str] = []
    #: 上一个带行尾注释的项，和那个注释的 # 在第几列：下一行的注释对齐的话算它的续行
    trailing: tuple[tuple[str, ...], int] | None = None
    #: 正在跳过缩进比它深的行（多行文字的内容、列表项里面的东西）
    skip_deeper: int | None = None
    seen_key = False

    for line in text.splitlines():
        stripped = line.strip()
        indent = len(line) - len(line.lstrip(" "))
        if skip_deeper is not None:
            if not stripped or indent > skip_deeper:
                continue
            skip_deeper = None
        if not stripped:
            if header is None and not seen_key and pending:
                header = pending
            pending, trailing = [], None
            continue
        if stripped.startswith("#"):
            words = stripped.lstrip("#").strip()
            if trailing is not None and indent >= trailing[1] - _ALIGN_SLACK:
                notes[trailing[0]].append(words)
            else:
                trailing = None
                pending.append(words)
            continue
        if stripped.startswith("- ") or stripped == "-":
            skip_deeper = indent
            pending, trailing = [], None
            continue
        parts = _split_key(line[indent:])
        if parts is None:
            continue
        key, rest = parts
        if not seen_key and header is None and pending:
            header, pending = pending, []  # 最上面那段注释归整个文件，不归第一项
        seen_key = True
        while stack and stack[-1][0] >= indent:
            stack.pop()
        stack.append((indent, key))
        path = tuple(name for _, name in stack)
        value, comment, column = _split_comment(rest)
        lines = [*pending]
        if comment:
            lines.append(comment)
        if lines:
            notes[path] = lines
        pending = []
        offset = len(line) - len(rest)
        trailing = (path, offset + column) if comment else None
        if _BLOCK_SCALAR.match(value):
            skip_deeper = indent
    return "\n".join(header or []), {path: "\n".join(lines) for path, lines in notes.items()}


def _kind(value: Any) -> tuple[str, str]:
    """一个值在表单里用什么控件 →（类型，列表里的项的类型）。"""
    if isinstance(value, bool):
        return "bool", ""
    if isinstance(value, int):
        return "int", ""
    if isinstance(value, float):
        return "float", ""
    if isinstance(value, str):
        return ("text" if "\n" in value or len(value) > _LONG_TEXT else "str"), ""
    if isinstance(value, list):
        if all(isinstance(item, int | float) and not isinstance(item, bool) for item in value):
            return "list", "number" if value else "str"
        if all(
            isinstance(item, str | int | float) and not isinstance(item, bool) for item in value
        ):
            return "list", "str"
        return "json", ""
    if value is None:
        return "json", ""
    return "json", ""


def _locked(name: str, path: tuple[str, ...], *values: Any) -> bool:
    """这一项是不是不许在网页上改。和保存时的检查用同一个函数（``locked_changes``）来判断，
    页面上显示成只读的和保存时被拒绝的才不会对不上。

    锁着的：它自己或者它上面某一层在名单里；或者它是个列表、里面套着名单里的项
    （列表只能整个换，换上去的列表里只要还带着那些项，保存就会被拒绝）。
    """
    for value in values or (None,):
        nested: Any = value if isinstance(value, list) else None
        for key in reversed(path):
            nested = {key: nested}
        if locked_changes(name, nested):
            return True
    return False


def build_schema(
    base: dict[str, Any],
    effective: dict[str, Any],
    notes: dict[tuple[str, ...], str],
    path: tuple[str, ...] = (),
    *,
    name: str = "",
) -> list[dict[str, Any]]:
    """表单的结构：一棵树，叶子是能填的项。``name`` 是这份配置的名字（有几项是按配置名锁的）。

    默认值里有的项按默认值定类型；默认值里没有、覆写里加出来的项按现在的值定。
    顺序按默认值文件里的书写顺序，加出来的排在后面。
    """
    fields: list[dict[str, Any]] = []
    for key in [*base, *(k for k in effective if k not in base)]:
        here = (*path, str(key))
        sample = base[key] if key in base else effective[key]
        field: dict[str, Any] = {
            "key": str(key),
            "path": list(here),
            "description": notes.get(here, ""),
            "added": key not in base,
            "locked": _locked(name, here, base.get(key), effective.get(key)),
        }
        if isinstance(sample, dict):
            default, now = base.get(key), effective.get(key)
            field["kind"] = "group"
            field["children"] = build_schema(
                default if isinstance(default, dict) else {},
                now if isinstance(now, dict) else {},
                notes,
                here,
                name=name,
            )
        else:
            field["kind"], field["items"] = _kind(sample)
        fields.append(field)
    return fields


def count_leaves(override: dict[str, Any]) -> int:
    """覆写里改了几项（数到最里面的值）。"""
    return sum(
        count_leaves(value) if isinstance(value, dict) and value else 1
        for value in override.values()
    )
