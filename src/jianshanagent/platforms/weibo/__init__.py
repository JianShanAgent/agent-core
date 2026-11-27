"""微博的离线标准资料适配器；从不读取原帖 URL 或附件。"""

from __future__ import annotations

import ipaddress
import re
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from jianshanagent.core.sources import SourceSetup, source_instance
from jianshanagent.plugins.knowledge.models import KIND_DISCUSSION, KIND_POST, Block, Document

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
    identifier = _string(value, name, 128)
    if not _ID.fullmatch(identifier):
        raise ValueError(f"{name} 必须是 1..32 位 ASCII 原生数字帖 ID，不能使用 URL 的 base62 别名")
    return identifier


def _url(raw: Any) -> str:
    value = _string(raw, "url", 4096)
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


_DISCUSSION_FIELDS = {
    "version",
    "id",
    "packet_id",
    "post_id",
    "post_author_id",
    "post_url",
    "nodes",
}
_NODE_FIELDS = {
    "id",
    "root_id",
    "parent_id",
    "reply_to_id",
    "author_id",
    "author_name",
    "published_at",
    "text",
    "media_refs",
}
MAX_DISCUSSION_NODES = 64


def _public_url(value: Any) -> str:
    url = _url(value)
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    if parsed.scheme != "https" or parsed.port not in (None, 443) or "." not in host:
        raise ValueError("讨论引用只允许公开 HTTPS URL")
    if host.endswith((".localhost", ".local", ".internal")):
        raise ValueError("讨论引用不能使用本地地址")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if not address.is_global:
            raise ValueError("讨论引用不能使用非公开地址")
    return url


def _post_url(value: Any, post_id: str, uid: str) -> str:
    url = _public_url(value)
    parsed = urlsplit(url)
    # URL 只用于出处，不下载页面。归属必须另由已发布的父来源原始帖子证明。
    if parsed.query or parsed.fragment:
        raise ValueError("post_url 必须是无查询串及锚点的微博原帖 URL")
    if parsed.hostname == "weibo.com":
        parts = parsed.path.strip("/").split("/")
        valid = len(parts) == 2 and (
            (parts[0] == uid and re.fullmatch(r"[A-Za-z0-9]{1,32}", parts[1]))
            or (parts[0] == "detail" and parts[1] == post_id)
        )
    else:
        valid = parsed.hostname == "m.weibo.cn" and parsed.path == f"/detail/{post_id}"
    if not valid:
        raise ValueError("post_url 必须定位到已关联账号的微博原帖")
    return url


def _validate_node_relations(by_id: dict[str, dict[str, Any]]) -> None:
    """也检查跨物理片段已知的关系；缺席的父/根节点仍保留为未知外部节点。"""
    for node in by_id.values():
        root = by_id.get(node["root_id"])
        if root and root["root_id"] != root["id"]:
            raise ValueError("讨论根节点关系矛盾")
        for relation in ("parent_id", "reply_to_id"):
            related = by_id.get(node[relation])
            if related and related["root_id"] != node["root_id"]:
                raise ValueError("讨论节点跨楼关系矛盾")
        visited = {node["id"]}
        cursor = node
        while cursor.get("parent_id") in by_id:
            cursor = by_id[cursor["parent_id"]]
            if cursor["id"] in visited:
                raise ValueError("讨论父节点关系成环")
            visited.add(cursor["id"])


class WeiboSourceAdapter:
    def __init__(self, setup: SourceSetup):
        self.setup = setup

    def _discussion(self, row: dict[str, Any]) -> Document:
        if set(row) - _DISCUSSION_FIELDS:
            raise ValueError("讨论记录含未知或受保护字段")
        source = self.setup.source
        parent_id = source.raw.get("discussion_of")
        if source.role != "community" or not isinstance(parent_id, str) or not parent_id:
            raise ValueError("version 2 讨论仅用于有 discussion_of 的 community 来源")
        parent = source_instance(self.setup.config, parent_id)
        collector = parent.raw.get("collector")
        if parent.platform.id != "weibo" or not isinstance(collector, dict):
            raise ValueError("讨论父来源必须是配置了 collector.uid 的微博来源")
        uid = _identifier(collector.get("uid"), "collector.uid")
        post_id = _identifier(row.get("post_id"), "post_id")
        if _identifier(row.get("post_author_id"), "post_author_id") != uid:
            raise ValueError("讨论所属帖子作者与关联 UID 不符")
        # 不相信导入记录自称的 post_author_id；只读已发布父帖，禁止网络取证或执行评论。
        from jianshanagent.plugins.knowledge.source_import_v2 import get_source_row

        parent_row = get_source_row(self.setup.config, parent_id, post_id)
        if (
            not isinstance(parent_row, dict)
            or type(parent_row.get("version")) is not int
            or parent_row.get("version") != 1
            or parent_row.get("id") != post_id
            or parent_row.get("author_id") != uid
        ):
            raise ValueError("讨论归属无法由已发布的父来源帖子验证")
        packet_id = _string(row.get("id"), "id", 65)
        match = re.fullmatch(r"([0-9]{1,32}):([0-9]{1,32})", packet_id)
        if not match or match[1] != post_id:
            raise ValueError("讨论 id 必须为 post_id:seed_comment_id")
        logical_packet_id = _string(row.get("packet_id", packet_id), "packet_id", 65)
        logical_match = re.fullmatch(r"([0-9]{1,32}):([0-9]{1,32})", logical_packet_id)
        if not logical_match or logical_match[1] != post_id:
            raise ValueError("packet_id 必须为同一 post_id 的稳定逻辑包身份")
        post_url = _post_url(row.get("post_url"), post_id, uid)
        path = urlsplit(post_url).path.strip("/").split("/")
        if path[0] == uid and post_url != parent_row.get("url"):
            raise ValueError("post_url 与已发布父帖的原生别名 URL 不符")
        raw_nodes = row.get("nodes")
        if not isinstance(raw_nodes, list) or not 1 <= len(raw_nodes) <= MAX_DISCUSSION_NODES:
            raise ValueError(f"nodes 必须是 1..{MAX_DISCUSSION_NODES} 项的有界数组")
        nodes: list[dict[str, Any]] = []
        by_id: dict[str, dict[str, Any]] = {}
        text_chars = 0
        for raw in raw_nodes:
            if (
                not isinstance(raw, dict)
                or set(raw) - _NODE_FIELDS
                or _NODE_FIELDS - {"media_refs"} - set(raw)
            ):
                raise ValueError("讨论节点含未知字段或缺少必要字段")
            node: dict[str, Any] = {
                name: _identifier(raw.get(name), f"nodes.{name}")
                for name in ("id", "root_id", "author_id")
            }
            for name in ("parent_id", "reply_to_id"):
                node[name] = (
                    _identifier(raw[name], f"nodes.{name}") if raw[name] is not None else None
                )
            if node["id"] in by_id:
                raise ValueError("讨论节点 ID 重复")
            if node["id"] in (node["parent_id"], node["reply_to_id"]):
                raise ValueError("讨论节点不能回复自身")
            if node["root_id"] == node["id"] and node["parent_id"] is not None:
                raise ValueError("根评论不能有父评论")
            node["author_name"] = _string(raw["author_name"], "nodes.author_name", 200)
            node["published_at"] = _timestamp(raw["published_at"], "nodes.published_at")
            if node["published_at"] is None:
                raise ValueError("讨论节点必须有真实带时区发表时间")
            # 标准行的 text 已由采集器规范化，不再裁切/strip，保证长评论可逐字符还原。
            text = raw["text"]
            _string(text, "nodes.text")
            if not text.strip():
                raise ValueError("讨论节点正文不能为空")
            node["text"] = text
            text_chars += len(text)
            if text_chars > MAX_TEXT_CHARS:
                raise ValueError("讨论包总正文超过长度限制")
            if "media_refs" in raw:
                refs = raw["media_refs"]
                if not isinstance(refs, list) or len(refs) > 16:
                    raise ValueError("media_refs 必须为至多 16 项的公开 URL 数组")
                node["media_refs"] = [_public_url(ref) for ref in refs]
            node["url"] = f"{post_url}#comment-{node['id']}"
            node["is_target_reply"] = node["author_id"] == uid
            by_id[node["id"]] = node
            nodes.append(node)
        if match[2] not in by_id:
            raise ValueError("讨论包必须包含 seed_comment_id 对应节点")
        if "packet_id" in row and nodes[0]["id"] != match[2]:
            raise ValueError("物理讨论片段 id 必须使用本片第一个原生节点")
        _validate_node_relations(by_id)
        key = f"weibo:discussion:{packet_id}"
        published = max((node["published_at"] for node in nodes), key=datetime.fromisoformat)
        return Document(
            doc_key=key,
            kind=KIND_DISCUSSION,
            title=f"微博 {post_id} 的社区讨论（片段 {match[2]}）",
            url=post_url,
            source=f"微博·{source.label}·社区讨论（非独立官方佐证）",
            date=published[:10],
            blocks=[Block("para", text=node["text"]) for node in nodes],
            meta={
                "source_platform": "weibo",
                "source_role": "community",
                "canonical_document_id": key,
                "packet_id": logical_packet_id,
                "logical_packet_id": logical_packet_id,
                "fragment_id": packet_id,
                "discussion_of": parent_id,
                "post_id": post_id,
                "post_url": post_url,
                "post_author_id": uid,
                "evidence_group": f"weibo:{post_id}",
                "published_at": published,
                "author_ids": list(dict.fromkeys(node["author_id"] for node in nodes)),
                "discussion_nodes": nodes,
                "node_ids": [node["id"] for node in nodes],
                "target_reply_ids": [node["id"] for node in nodes if node["is_target_reply"]],
                "untrusted_content": True,
            },
        )

    def normalize(self, rows: list[dict[str, Any]]) -> list[Document]:
        documents: list[Document] = []
        seen: set[str] = set()
        seen_nodes: set[str] = set()
        discussion_nodes: dict[str, dict[str, Any]] = {}
        node_posts: dict[str, str] = {}
        packet_sizes: dict[str, int] = {}
        for index, row in enumerate(rows, 1):
            try:
                if (
                    isinstance(row, dict)
                    and type(row.get("version")) is int
                    and row["version"] == 2
                ):
                    discussion = self._discussion(row)
                    if discussion.doc_key in seen:
                        raise ValueError("批次内重复或冲突讨论 ID")
                    node_ids = set(discussion.meta["node_ids"])
                    if seen_nodes.intersection(node_ids):
                        raise ValueError("批次内讨论节点不能重复分配到多个包")
                    logical = discussion.meta["logical_packet_id"]
                    packet_sizes[logical] = packet_sizes.get(logical, 0) + len(node_ids)
                    if packet_sizes[logical] > MAX_DISCUSSION_NODES:
                        raise ValueError("逻辑讨论包的全部物理片段合计不得超过 64 节点")
                    discussion_nodes.update(
                        {node["id"]: node for node in discussion.meta["discussion_nodes"]}
                    )
                    node_posts.update(dict.fromkeys(node_ids, discussion.meta["post_id"]))
                    seen_nodes.update(node_ids)
                    seen.add(discussion.doc_key)
                    documents.append(discussion)
                    continue
                if self.setup.source.raw.get("discussion_of"):
                    raise ValueError("关联讨论来源只接受 version 2 讨论记录")
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
        for node_id, node in discussion_nodes.items():
            for relation in ("root_id", "parent_id", "reply_to_id"):
                related = node.get(relation)
                if related in node_posts and node_posts[related] != node_posts[node_id]:
                    raise ValueError("讨论节点关系不能跨微博原帖")
        _validate_node_relations(discussion_nodes)
        return documents


def create_source_adapter(setup: SourceSetup) -> WeiboSourceAdapter:
    return WeiboSourceAdapter(setup)
