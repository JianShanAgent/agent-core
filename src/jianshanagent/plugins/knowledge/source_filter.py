"""请求级来源隔离与只读原文身份；不改历史主键、正文或向量。"""

from __future__ import annotations

import re
from dataclasses import dataclass, fields
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from jianshanagent.plugins.knowledge.store import KbChunk


@dataclass(frozen=True, slots=True)
class SourceFilter:
    """None 表示不限，空允许集合表示无结果；平台指内容原平台而非采集驱动。"""

    source_ids: tuple[str, ...] | None = None
    source_platforms: tuple[str, ...] | None = None
    author_ids: tuple[str, ...] | None = None
    exclude_source_ids: tuple[str, ...] = ()
    exclude_source_platforms: tuple[str, ...] = ()
    exclude_author_ids: tuple[str, ...] = ()
    any_of: tuple[SourceFilter, ...] = ()

    def __post_init__(self) -> None:
        for field in fields(self):
            value = getattr(self, field.name)
            if field.name == "any_of":
                if not isinstance(value, (tuple, list)) or any(
                    not isinstance(v, SourceFilter) for v in value
                ):
                    raise ValueError("any_of 必须是 SourceFilter 序列")
                object.__setattr__(self, field.name, tuple(value))
                continue
            if value is None and not field.name.startswith("exclude_"):
                continue
            if not isinstance(value, (tuple, list, set, frozenset)) or any(
                not isinstance(item, str) for item in value
            ):
                raise ValueError(f"{field.name} 必须是字符串序列")
            object.__setattr__(self, field.name, tuple(dict.fromkeys(value)))

    def allows(self, chunk: KbChunk) -> bool:
        for value, allowed, excluded in (
            (chunk.wiki, self.source_ids, self.exclude_source_ids),
            (
                str(chunk.meta.get("source_platform") or ""),
                self.source_platforms,
                self.exclude_source_platforms,
            ),
            (str(chunk.meta.get("author_id") or ""), self.author_ids, self.exclude_author_ids),
        ):
            if (allowed is not None and value not in allowed) or value in excluded:
                return False
        return not self.any_of or any(branch.allows(chunk) for branch in self.any_of)

    def intersect(self, other: SourceFilter | None) -> SourceFilter:
        if other is None:
            return self
        values: dict[str, Any] = {}
        for field in fields(self):
            left, right = getattr(self, field.name), getattr(other, field.name)
            if field.name == "any_of":
                if left and right:
                    values[field.name] = tuple(a.intersect(b) for a in left for b in right)
                else:
                    values[field.name] = left or right
            elif field.name.startswith("exclude_"):
                values[field.name] = tuple(dict.fromkeys((*left, *right)))
            elif left is None:
                values[field.name] = right
            elif right is None:
                values[field.name] = left
            else:
                values[field.name] = tuple(item for item in left if item in right)
        return SourceFilter(**values)

    def with_legacy_source(self, source_id: str | None) -> SourceFilter:
        """仅首Wiki拥有旧空namespace；只扩展显式引用该Wiki的来源条件。"""
        if not source_id:
            return self
        values = {field.name: getattr(self, field.name) for field in fields(self)}
        for name in ("source_ids", "exclude_source_ids"):
            allowed = values[name]
            if allowed is not None and source_id in allowed and "" not in allowed:
                values[name] = (*allowed, "")
        values["any_of"] = tuple(branch.with_legacy_source(source_id) for branch in self.any_of)
        return SourceFilter(**values)

    def sql(self, alias: str, start: int = 1) -> tuple[str, list[Any]]:
        """内部固定列名，所有用户值作为绑定参数；条件必须在候选/LIMIT 前应用。"""
        clauses: list[str] = []
        args: list[Any] = []
        for column, allowed, excluded in (
            (f"{alias}.wiki", self.source_ids, self.exclude_source_ids),
            (
                f"coalesce({alias}.meta->>'source_platform', '')",
                self.source_platforms,
                self.exclude_source_platforms,
            ),
            (f"coalesce({alias}.meta->>'author_id', '')", self.author_ids, self.exclude_author_ids),
        ):
            for values, operator in ((allowed, "= ANY"), (excluded or None, "<> ALL")):
                if values is not None:
                    clauses.append(f"{column} {operator}(${start + len(args)}::text[])")
                    args.append(list(values))
        if self.any_of:
            branches = []
            for branch in self.any_of:
                condition, branch_args = branch.sql(alias, start + len(args))
                branches.append(f"({condition})")
                args.extend(branch_args)
            clauses.append("(" + " OR ".join(branches) + ")")
        return " AND ".join(clauses) or "TRUE", args


def canonical_document_id(chunk: KbChunk) -> str | None:
    """只认有原平台字段的数字微博本帖；origin_id 从来不是本帖身份。"""
    if chunk.meta.get("source_platform") != "weibo":
        return None
    explicit = chunk.meta.get("canonical_document_id")
    if isinstance(explicit, str) and re.fullmatch(r"weibo:[0-9]+", explicit):
        return explicit
    local = chunk.doc_key.removeprefix(f"{chunk.wiki}:") if chunk.wiki else chunk.doc_key
    match = re.fullmatch(r"(?:huiji:)?weibo:([0-9]+)", local)
    return f"weibo:{match[1]}" if match else None


def provenance(chunk: KbChunk) -> dict[str, Any]:
    """研究层可复用的出处；未知角色不升级成官方，转载依赖与本帖身份分开。"""
    role = chunk.meta.get("source_role")
    return {
        "source_id": chunk.wiki,
        "source_platform": chunk.meta.get("source_platform"),
        "canonical_document_id": canonical_document_id(chunk),
        "author_id": chunk.meta.get("author_id"),
        "author_name": chunk.meta.get("author_name"),
        "source_role": role if role in ("unknown", "official", "community") else "unknown",
        "post_id": chunk.meta.get("post_id"),
        "origin_id": chunk.meta.get("origin_id"),
        "repost_of": chunk.meta.get("repost_of"),
        "repost": chunk.meta.get("repost"),
        "url": chunk.url,
        "captures": chunk.meta.get("captures")
        or [{"source_id": chunk.wiki, "doc_key": chunk.doc_key, "url": chunk.url}],
    }


def canonical_sql(alias: str) -> str:
    """与 canonical_document_id 同义，不依赖 URL、标题或正文相似度。"""
    local = (
        f"CASE WHEN {alias}.wiki <> '' AND starts_with({alias}.doc_key, {alias}.wiki || ':') "
        f"THEN substr({alias}.doc_key, length({alias}.wiki) + 2) ELSE {alias}.doc_key END"
    )
    return f"""CASE WHEN {alias}.meta->>'source_platform' = 'weibo' THEN
        CASE WHEN {alias}.meta->>'canonical_document_id' ~ '^weibo:[0-9]+$'
             THEN {alias}.meta->>'canonical_document_id'
             WHEN ({local}) ~ '^weibo:[0-9]+$' THEN ({local})
             WHEN ({local}) ~ '^huiji:weibo:[0-9]+$'
             THEN substr(({local}), 7) END END"""
