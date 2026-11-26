"""微博的离线标准资料适配器；从不读取原帖 URL 或附件。"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from jianshanagent.core.sources import SourceSetup
from jianshanagent.plugins.knowledge.models import KIND_POST, Block, Document

MAX_TEXT_CHARS = 100_000
MAX_BLOCKS = 1000
_FIELDS = {
    "version",
    "id",
    "text",
    "blocks",
    "title",
    "url",
    "published_at",
    "author_id",
    "author_name",
    "repost",
    "edited_at",
    "collected_at",
    "collection",
}
_REPOST_FIELDS = {"id", "url", "text", "author_id", "author_name", "published_at"}
_ID = re.compile(r"[0-9]{1,32}\Z")


def _string(value: Any, name: str, limit: int = MAX_TEXT_CHARS) -> str:
    if not isinstance(value, str) or len(value) > limit or "\x00" in value:
        raise ValueError(f"{name} 必须是至多 {limit} 字的字符串且不能含 NUL")
    return value.strip()


def _identifier(value: Any, name: str = "id") -> str:
    value = _string(value, name, 128)
    if not _ID.fullmatch(value):
        raise ValueError(f"{name} 必须是 1..32 位 ASCII 原生数字帖 ID，不能使用 URL 的 base62 别名")
    return value


def _url(value: Any) -> str:
    value = _string(value, "url", 4096)
    if not value:
        return ""
    if any(ord(c) <= 32 or ord(c) == 127 for c in value) or "\\" in value:
        raise ValueError("url 含不安全字符")
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"https", "http"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("url 只允许不含凭据的 http(s) 原文链接")
        _ = parsed.port
    except ValueError:
        raise ValueError("url 只允许合法且不含凭据的 http(s) 原文链接") from None
    return value


def _timestamp(value: Any, name: str) -> str | None:
    if value is None:
        return None
    value = _string(value, name, 80)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{name} 必须是带时区的 ISO8601 时间或 null") from None
    if "T" not in value or parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} 必须是带时区的 ISO8601 时间或 null")
    return parsed.isoformat()


def _blocks(row: dict[str, Any]) -> list[Block]:
    blocks: list[Block] = []
    if "text" in row:
        text = _string(row["text"], "text")
        if text:
            blocks.append(Block("para", text=text))
    if "blocks" in row:
        if "text" in row:
            raise ValueError("text 与 blocks 只能提供一种，避免重复正文")
        raw = row["blocks"]
        if not isinstance(raw, list) or len(raw) > MAX_BLOCKS:
            raise ValueError(f"blocks 必须是至多 {MAX_BLOCKS} 项的数组")
        for item in raw:
            if not isinstance(item, dict) or set(item) - {"kind", "text", "level", "items"}:
                raise ValueError("blocks 项含未知字段")
            kind = item.get("kind")
            if kind not in {"para", "heading", "list", "pre"}:
                raise ValueError("blocks.kind 仅支持 para/heading/list/pre")
            text = _string(item.get("text", ""), "blocks.text")
            level = item.get("level", 1 if kind == "heading" else 0)
            if type(level) is not int or not 0 <= level <= 6 or (kind == "heading" and not level):
                raise ValueError("blocks.level 必须为合法标题层级")
            items = item.get("items", [])
            if not isinstance(items, list) or len(items) > MAX_BLOCKS:
                raise ValueError("blocks.items 必须是有界字符串数组")
            items = [_string(v, "blocks.items") for v in items]
            if kind == "list" and text:
                raise ValueError("list 块只使用 items")
            if kind != "list" and items:
                raise ValueError("非 list 块不能包含 items")
            blocks.append(Block(kind, text=text, level=level, items=items))
    if not any(b.render().strip() for b in blocks if b.kind != "heading"):
        raise ValueError("帖子正文不能为空")
    if sum(b.size() for b in blocks) > MAX_TEXT_CHARS:
        raise ValueError("帖子正文超过长度限制")
    return blocks


class WeiboSourceAdapter:
    def __init__(self, setup: SourceSetup):
        self.setup = setup

    def normalize(self, rows: list[dict[str, Any]]) -> list[Document]:
        documents: list[Document] = []
        seen: set[str] = set()
        for index, row in enumerate(rows, 1):
            try:
                if not isinstance(row, dict) or set(row) - _FIELDS:
                    raise ValueError("记录含未知或受保护字段")
                if type(row.get("version")) is not int or row["version"] != 1:
                    raise ValueError("version 必须为 1")
                post_id = _identifier(row.get("id"))
                if post_id in seen:
                    raise ValueError(f"批次内重复或冲突 ID：{post_id}")
                seen.add(post_id)
                blocks = _blocks(row)
                published = _timestamp(row.get("published_at"), "published_at")
                author_id = _string(row.get("author_id", ""), "author_id", 128)
                author_name = _string(row.get("author_name", ""), "author_name", 200)
                title = _string(row.get("title", ""), "title", 500) or f"微博 {post_id}"
                meta: dict[str, Any] = {
                    "source_platform": "weibo",
                    "canonical_document_id": f"weibo:{post_id}",
                    "post_id": post_id,
                    "author_id": author_id,
                    "author_name": author_name,
                    "source_role": self.setup.source.role,
                    "published_at": published,
                }
                for name in ("edited_at", "collected_at"):
                    if name in row:
                        meta[name] = _timestamp(row[name], name)
                if "collection" in row:
                    meta["collection"] = _string(row["collection"], "collection", 500)
                if "repost" in row:
                    repost = row["repost"]
                    if not isinstance(repost, dict) or set(repost) - _REPOST_FIELDS:
                        raise ValueError("repost 含未知字段")
                    rid = _identifier(repost.get("id"), "repost.id")
                    if rid == post_id:
                        raise ValueError("不能转发自身")
                    ref = {
                        "id": rid,
                        "canonical_document_id": f"weibo:{rid}",
                        "url": _url(repost.get("url", "")),
                        "text": _string(repost.get("text", ""), "repost.text"),
                        "author_id": _string(repost.get("author_id", ""), "repost.author_id", 128),
                        "author_name": _string(
                            repost.get("author_name", ""), "repost.author_name", 200
                        ),
                        "published_at": _timestamp(
                            repost.get("published_at"), "repost.published_at"
                        ),
                    }
                    meta["repost"] = ref
                    meta["origin_id"] = rid
                    meta["repost_of"] = f"weibo:{rid}"
                    if ref["text"]:
                        blocks.extend(
                            [
                                Block("heading", text=f"转发原帖 {rid}（非独立佐证）", level=1),
                                Block("para", text=ref["text"]),
                            ]
                        )
                role = {"official": "官方来源", "community": "社区讨论", "unknown": "身份未核实"}
                source = f"微博·{self.setup.source.label}·{role[self.setup.source.role]}"
                documents.append(
                    Document(
                        doc_key=f"weibo:{post_id}",
                        kind=KIND_POST,
                        title=title,
                        url=_url(row.get("url", "")),
                        source=source,
                        date=published[:10] if published else None,
                        blocks=blocks,
                        meta=meta,
                    )
                )
            except ValueError as exc:
                raise ValueError(f"第 {index} 条：{exc}") from None
        return documents


def create_source_adapter(setup: SourceSetup) -> WeiboSourceAdapter:
    return WeiboSourceAdapter(setup)
