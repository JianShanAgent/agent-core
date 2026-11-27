"""知识库的存储和两种检索（向量、关键词）。

线上用 Postgres（表见 migrations/0003_knowledge.sql），向量放 pgvector；
没有数据库时（测试、本机调试小样本）用进程内的实现，两者行为一致。

- 向量检索：库里的向量都已归一化，所以直接按内积排，内积就是余弦相似度。
  目前不建索引、整表精确比（1.4 万片实测够快），不会漏。
- 关键词检索分两步：先按「含有哪些查询词」粗选出一批（每个词按稀有程度给权重，权重相加，
  数据库里走倒排索引，很快），再对这一批按 BM25 细算（看每个词出现了几次、这一片有多长）。
  只粗选不细算的话，含有同样几个词的切片得分完全一样，谁排前面全凭运气。

导入（``sync``）按 wiki 对齐：传进来的是一个 wiki 当前的全部切片，库里这个 wiki 多出来的删掉，
内容没变的不动；别的 wiki 的切片不碰。

部署挂了几个 wiki，它们的切片就都在这一张表里，检索时一起找。每片记着出自哪个 wiki
（``wiki``，``core.wiki.yaml`` 里的 id），库里的 ``chunk_id``、``doc_key`` 前面带着 ``<wiki>:``
（见 :func:`scoped`）——切片文件里的 id 是各个 wiki 自己编的，页面号会重。
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Protocol

from jianshanagent.core.db import Database
from jianshanagent.core.embedding import dot
from jianshanagent.plugins.knowledge.news import (
    PUBLISHED_AT_PATTERN,
    NewsQuery,
    NewsResult,
    select_recent_documents,
)
from jianshanagent.plugins.knowledge.source_filter import SourceFilter, canonical_sql
from jianshanagent.plugins.knowledge.terms import bm25_score, extract_terms, term_weights

#: （切片，向量或 None）。没有向量的切片只能被关键词查到
Record = tuple["KbChunk", Sequence[float] | None]

#: ``kb_meta`` 表里的键：一个 wiki 的概况是 ``stats:<wiki>``，
#: 自动更新的状态是 ``sync:<wiki>``（sync.py 写，管理后台读）
STATS_KEY = "stats"
SYNC_STATUS_KEY = "sync"
_UPSERT_BATCH = 100
# 所有修改 kb_chunks/kb_terms 的路径共用；先取得锁再读取快照，避免 full sync
# 的全量重算和文档差量互相覆盖（也覆盖 keep_only）。事务结束自动释放。
_TERMS_WRITE_LOCK = 0x4A53414B425446
#: 关键词检索粗选多少片来细算
KEYWORD_POOL = 100


def scoped(wiki: str, local_id: str) -> str:
    """库里的 id：``<wiki>:<切片文件里的 id>``。没说是哪个 wiki（测试里）就是文件里的 id。"""
    return f"{wiki}:{local_id}" if wiki else local_id


def _check_namespace(chunk: KbChunk, wiki: str) -> None:
    if chunk.wiki != wiki or (
        wiki and not all(value.startswith(f"{wiki}:") for value in (chunk.chunk_id, chunk.doc_key))
    ):
        raise ValueError("切片来源及主键前缀必须属于目标来源")


def _document_scope(wiki: str, doc_keys: Sequence[str]) -> set[str]:
    """局部发布必须显式指定非空来源和完整文档主键；空集合表示不发布文档。"""
    if not isinstance(wiki, str) or not wiki or ":" in wiki or wiki.strip() != wiki:
        raise ValueError("文档同步必须指定有效的非空来源")
    if not isinstance(doc_keys, Sequence) or isinstance(doc_keys, (str, bytes)):
        raise ValueError("doc_keys 必须是完整文档主键的序列")
    prefix = f"{wiki}:"
    if any(
        not isinstance(key, str) or not key.startswith(prefix) or key == prefix for key in doc_keys
    ):
        raise ValueError("文档主键必须带目标来源前缀且本地标识非空")
    return set(doc_keys)


def meta_key(kind: str, wiki: str) -> str:
    """``kb_meta`` 里一个 wiki 的概况（``stats``）或更新状态（``sync``）的键。"""
    return f"{kind}:{wiki}"


@dataclass(frozen=True, slots=True)
class KbChunk:
    chunk_id: str
    doc_key: str
    kind: str
    title: str
    #: 首行是标题路径（公告另带日期和来源），后面是正文
    text: str
    url: str = ""
    source: str = ""
    section_path: tuple[str, ...] = ()
    seq: int = 0
    date: str | None = None
    date_inferred: str | None = None
    entities: list[dict[str, Any]] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)
    #: 出自哪个 wiki（``core.wiki.yaml`` 里的 id）
    wiki: str = ""

    @property
    def source_id(self) -> str:
        """通用来源只读别名；wiki 列及历史前缀保持不变。"""
        return self.wiki

    @property
    def local_id(self) -> str:
        """切片文件（``chunks.jsonl``）里的 id：不带 wiki 的那一段。"""
        return self.chunk_id.removeprefix(f"{self.wiki}:") if self.wiki else self.chunk_id

    @classmethod
    def from_json(cls, row: dict[str, Any], wiki: str = "") -> KbChunk:
        """``chunks.jsonl`` 的一行。``wiki``：这个文件是哪个 wiki 的。"""
        return cls(
            wiki=wiki,
            chunk_id=scoped(wiki, str(row["chunk_id"])),
            doc_key=scoped(wiki, str(row["doc_key"])),
            kind=str(row["type"]),
            title=str(row["title"]),
            text=str(row["text"]),
            url=str(row.get("url") or ""),
            source=str(row.get("source") or ""),
            section_path=tuple(str(s) for s in row.get("section_path") or ()),
            seq=int(row.get("seq") or 0),
            date=row.get("date") or None,
            date_inferred=row.get("date_inferred") or None,
            entities=list(row.get("entities") or []),
            meta=dict(row.get("meta") or {}),
        )

    def row_hash(self, fingerprint: str | None) -> str:
        """这一行存进库里的全部内容的哈希（``fingerprint`` 为 None 表示没有向量）。"""
        payload = [
            self.chunk_id,
            self.doc_key,
            self.kind,
            self.title,
            self.text,
            self.url,
            self.source,
            list(self.section_path),
            self.seq,
            self.date,
            self.date_inferred,
            self.entities,
            self.meta,
            fingerprint,
        ]
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class Hit:
    chunk: KbChunk
    score: float


@dataclass(frozen=True, slots=True)
class ChunkPlace:
    """一个切片在它所在页面里的位置（不带正文）。"""

    chunk_id: str
    doc_key: str
    section_path: tuple[str, ...]
    #: 在页面里排第几
    seq: int
    #: 正文的字数
    chars: int


@dataclass(frozen=True, slots=True)
class StoreInfo:
    chunks: int = 0
    with_vectors: int = 0
    #: 库里向量的指纹；没有向量时为 None。整库的概况里，各个 wiki 的指纹不一样时也是 None
    fingerprint: str | None = None
    loaded_at: str | None = None
    #: 切片的平均字数（关键词打分时用来衡量一片算长还是算短）
    average_chars: float = 0.0
    #: 整库的概况里才有：各个 wiki 的概况，wiki 的 id → 概况
    wikis: dict[str, StoreInfo] = field(default_factory=dict)

    def has_vectors_of(self, fingerprint: str) -> bool:
        """库里有没有用 ``fingerprint`` 这个指纹算的向量。"""
        if self.wikis:
            return any(info.has_vectors_of(fingerprint) for info in self.wikis.values())
        return bool(self.with_vectors) and self.fingerprint == fingerprint


def combined_info(wikis: dict[str, StoreInfo]) -> StoreInfo:
    """各个 wiki 的概况 → 整库的概况。没标出处的那一份（分 wiki 之前导入的）空了就不算。"""
    wikis = {wiki: info for wiki, info in wikis.items() if wiki or info.chunks}
    with_data = [info for info in wikis.values() if info.chunks]
    chunks = sum(info.chunks for info in with_data)
    fingerprints = {info.fingerprint for info in with_data if info.with_vectors}
    return StoreInfo(
        chunks=chunks,
        with_vectors=sum(info.with_vectors for info in with_data),
        fingerprint=next(iter(fingerprints)) if len(fingerprints) == 1 else None,
        loaded_at=max((info.loaded_at for info in with_data if info.loaded_at), default=None),
        average_chars=sum(info.average_chars * info.chunks for info in with_data) / max(chunks, 1),
        wikis=dict(wikis),
    )


@dataclass(slots=True)
class SyncReport:
    total: int = 0
    upserted: int = 0
    unchanged: int = 0
    deleted: int = 0
    with_vectors: int = 0
    terms: int = 0
    seconds: float = 0.0


def _best_by_bm25(
    candidates: Iterable[KbChunk], weights: dict[str, float], average_chars: float, limit: int
) -> list[Hit]:
    scored = [(bm25_score(weights, chunk.text, average_chars), chunk) for chunk in candidates]
    scored.sort(key=lambda item: (-item[0], item[1].chunk_id))
    return [Hit(chunk, score) for score, chunk in scored[:limit] if score > 0]


class KnowledgeStore(Protocol):
    async def sync(
        self,
        records: Iterable[Record],
        *,
        fingerprint: str,
        wiki: str = "",
        replace_unlabelled: bool = False,
    ) -> SyncReport:
        """把库里 ``wiki`` 这个 wiki 的切片对齐成 ``records``（它当前的全部切片）。

        ``fingerprint`` 是这批向量的指纹。``replace_unlabelled``：库里没标出处的切片
        （分 wiki 之前导入的）算它的，一起换掉——导入主 wiki 时用。
        """

    async def sync_documents(
        self,
        records: Iterable[Record],
        *,
        fingerprint: str,
        wiki: str,
        doc_keys: Sequence[str],
    ) -> SyncReport:
        """事务替换完整 scoped ``doc_keys`` 内的全部切片，其他文档/来源不动。

        ``records`` 可为空（恢复此前不存在的文档）；越界记录及主键冲突整批失败。
        返回的 total/with_vectors 仅计本批；info() 仍统计整个来源。
        """

    async def info(self) -> StoreInfo:
        """整库的概况；各个 wiki 的在 ``wikis`` 里。"""

    async def resource_usage(self, source_ids: Collection[str]) -> dict[str, int]:
        """PG资源快照：database_bytes含全库表/索引/TOAST，wal_position_bytes为LSN字节。

        共享索引、WAL不能精确归属来源，预算用两者前后差量保守计；
        source_rows_bytes只供诊断，不能代替物理空间。无法测量须抛错。
        pg_started_at为Unix epoch整数微秒，用于使宿主同盘证明在PG重启后失效。
        """

    async def keep_only(self, wikis: Collection[str]) -> int:
        """删掉不在 ``wikis`` 里的 wiki 的切片（从配置里拿掉了的 wiki）；返回删了几片。

        没标出处的切片不动：它们等着主 wiki 下一次导入时换掉。
        """

    async def vector_search(
        self,
        vector: Sequence[float],
        *,
        limit: int,
        titles: Sequence[str] | None = None,
        wiki: str | None = None,
        fingerprint: str | None = None,
        exclude_wikis: Collection[str] | None = None,
        source_filter: SourceFilter | None = None,
    ) -> list[Hit]:
        """按向量相似度从高到低。``titles``：只在这些页面的切片里找；``wiki``：只在这个 wiki 里找。

        ``fingerprint``：只和用这个指纹算的向量比（各个 wiki 的向量可能不是同一批算的）。
        ``exclude_wikis``：这些 wiki 的切片不参与（卸下了挂载的）。名额仍由剩下的补满。
        """

    async def keyword_search(
        self,
        terms: Iterable[str],
        *,
        limit: int,
        max_df_ratio: float = 0.2,
        titles: Sequence[str] | None = None,
        wiki: str | None = None,
        exclude_wikis: Collection[str] | None = None,
        source_filter: SourceFilter | None = None,
    ) -> list[Hit]:
        """按关键词得分从高到低；一个查询词都不含的切片不返回。

        ``titles``、``wiki``、``exclude_wikis`` 和 :meth:`vector_search` 一样。
        """

    async def recent_documents(
        self, query: NewsQuery, *, exclude_wikis: Collection[str] | None = None
    ) -> NewsResult:
        """按可靠发布日期返回不同公告文档的首片，并报告同来源的日期覆盖情况。"""

    async def outline(
        self, chunk_ids: Collection[str], *, source_filter: SourceFilter | None = None
    ) -> list[ChunkPlace]:
        """这些切片所在的页面的全部切片（不带正文），按页面、页内先后排。库里没有的 id 不管。"""

    async def get(
        self, chunk_ids: Collection[str], *, source_filter: SourceFilter | None = None
    ) -> list[KbChunk]:
        """按 id 取切片；库里没有的不返回。"""

    async def page(
        self, title: str, *, url_prefix: str = "", source_filter: SourceFilter | None = None
    ) -> list[KbChunk]:
        """标题是 ``title`` 的页面的全部切片，按页面、页内先后排；库里没有这一页就是空的。

        ``url_prefix``：只要链接以它开头的（传 wiki 的页面地址前缀，同名的公告就不算）。
        """


class MemoryKnowledgeStore:
    """进程内实现：测试和小样本调试用。向量检索是逐条算的，不适合整库。"""

    def __init__(self) -> None:
        self._chunks: dict[str, KbChunk] = {}
        self._hashes: dict[str, str] = {}
        self._vectors: dict[str, Sequence[float]] = {}
        self._fingerprints: dict[str, str] = {}
        self._terms: dict[str, set[str]] = {}
        self._df: dict[str, int] = {}
        self._wikis: dict[str, StoreInfo] = {}

    async def sync(
        self,
        records: Iterable[Record],
        *,
        fingerprint: str,
        wiki: str = "",
        replace_unlabelled: bool = False,
    ) -> SyncReport:
        started = time.perf_counter()
        report = SyncReport()
        mine = {wiki, ""} if replace_unlabelled else {wiki}
        old = {chunk_id for chunk_id, chunk in self._chunks.items() if chunk.wiki in mine}
        chunks: dict[str, KbChunk] = {}
        hashes: dict[str, str] = {}
        vectors: dict[str, Sequence[float]] = {}
        for chunk, vector in records:
            _check_namespace(chunk, wiki)
            report.total += 1
            chunks[chunk.chunk_id] = chunk
            if vector is not None:
                vectors[chunk.chunk_id] = vector
            row_hash = chunk.row_hash(fingerprint if vector is not None else None)
            hashes[chunk.chunk_id] = row_hash
            if self._hashes.get(chunk.chunk_id) == row_hash:
                report.unchanged += 1
            else:
                report.upserted += 1
        report.deleted = len(old - set(chunks))
        for chunk_id in old:
            del self._chunks[chunk_id], self._hashes[chunk_id]
            self._vectors.pop(chunk_id, None)
            self._fingerprints.pop(chunk_id, None)
        self._chunks |= chunks
        self._hashes |= hashes
        self._vectors |= vectors
        self._fingerprints |= dict.fromkeys(vectors, fingerprint)
        self._terms = {chunk_id: extract_terms(c.text) for chunk_id, c in self._chunks.items()}
        self._df = {}
        for terms in self._terms.values():
            for term in terms:
                self._df[term] = self._df.get(term, 0) + 1
        report.with_vectors = len(vectors)
        report.terms = len(self._df)
        for gone in mine - {wiki}:
            self._wikis.pop(gone, None)
        self._wikis[wiki] = StoreInfo(
            chunks=len(chunks),
            with_vectors=len(vectors),
            fingerprint=fingerprint if vectors else None,
            loaded_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            average_chars=sum(len(c.text) for c in chunks.values()) / max(len(chunks), 1),
        )
        report.seconds = time.perf_counter() - started
        return report

    async def sync_documents(
        self,
        records: Iterable[Record],
        *,
        fingerprint: str,
        wiki: str,
        doc_keys: Sequence[str],
    ) -> SyncReport:
        started = time.perf_counter()
        scope = _document_scope(wiki, doc_keys)
        report = SyncReport()
        # 先验证全部输入，再修改内存；迭代器中途失败同样不留下半批数据。
        pending: dict[str, Record] = {}
        for chunk, vector in records:
            _check_namespace(chunk, wiki)
            if chunk.doc_key not in scope or chunk.chunk_id == f"{wiki}:":
                raise ValueError("切片不属于指定文档作用域")
            old = self._chunks.get(chunk.chunk_id)
            if chunk.chunk_id in pending or (
                old is not None and (old.wiki != wiki or old.doc_key not in scope)
            ):
                raise ValueError("切片主键重复或属于作用域外文档")
            pending[chunk.chunk_id] = chunk, vector
        hashes = {
            key: chunk.row_hash(fingerprint if vector is not None else None)
            for key, (chunk, vector) in pending.items()
        }
        terms = {key: extract_terms(chunk.text) for key, (chunk, _) in pending.items()}
        old_ids = {
            key
            for key, chunk in self._chunks.items()
            if chunk.wiki == wiki and chunk.doc_key in scope
        }
        for key in old_ids:
            for term in self._terms.pop(key):
                self._df[term] -= 1
                if self._df[term] == 0:
                    del self._df[term]
            if key not in pending:
                del self._chunks[key], self._hashes[key]
                self._vectors.pop(key, None)
                self._fingerprints.pop(key, None)
                report.deleted += 1
        for key, (chunk, vector) in pending.items():
            report.total += 1
            if self._hashes.get(key) == hashes[key]:
                report.unchanged += 1
            else:
                report.upserted += 1
            self._chunks[key], self._hashes[key], self._terms[key] = chunk, hashes[key], terms[key]
            if vector is not None:
                self._vectors[key], self._fingerprints[key] = vector, fingerprint
                report.with_vectors += 1
            else:
                self._vectors.pop(key, None)
                self._fingerprints.pop(key, None)
            for term in terms[key]:
                self._df[term] = self._df.get(term, 0) + 1
        if scope:
            mine = [chunk for chunk in self._chunks.values() if chunk.wiki == wiki]
            fingerprints = {
                self._fingerprints[c.chunk_id] for c in mine if c.chunk_id in self._vectors
            }
            self._wikis[wiki] = StoreInfo(
                chunks=len(mine),
                with_vectors=sum(c.chunk_id in self._vectors for c in mine),
                fingerprint=next(iter(fingerprints)) if len(fingerprints) == 1 else None,
                loaded_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                average_chars=sum(len(c.text) for c in mine) / max(len(mine), 1),
            )
        report.terms = len(self._df)
        report.seconds = time.perf_counter() - started
        return report

    async def resource_usage(self, source_ids: Collection[str]) -> dict[str, int]:
        raise RuntimeError("内存知识库无法测量 PostgreSQL 物理空间及 WAL，不能报告为零")

    async def info(self) -> StoreInfo:
        return combined_info(self._wikis)

    async def keep_only(self, wikis: Collection[str]) -> int:
        gone = [cid for cid, c in self._chunks.items() if c.wiki and c.wiki not in wikis]
        for chunk_id in gone:
            del self._chunks[chunk_id], self._hashes[chunk_id], self._terms[chunk_id]
            self._vectors.pop(chunk_id, None)
            self._fingerprints.pop(chunk_id, None)
        self._wikis = {w: info for w, info in self._wikis.items() if not w or w in wikis}
        self._df = {}
        for terms in self._terms.values():
            for term in terms:
                self._df[term] = self._df.get(term, 0) + 1
        return len(gone)

    def _within(
        self,
        chunk_id: str,
        titles: Sequence[str] | None,
        wiki: str | None,
        exclude_wikis: Collection[str] | None,
        source_filter: SourceFilter | None = None,
    ) -> bool:
        chunk = self._chunks[chunk_id]
        return (
            (titles is None or chunk.title in titles)
            and (wiki is None or chunk.wiki == wiki)
            and (not exclude_wikis or chunk.wiki not in exclude_wikis)
            and (source_filter is None or source_filter.allows(chunk))
        )

    async def vector_search(
        self,
        vector: Sequence[float],
        *,
        limit: int,
        titles: Sequence[str] | None = None,
        wiki: str | None = None,
        fingerprint: str | None = None,
        exclude_wikis: Collection[str] | None = None,
        source_filter: SourceFilter | None = None,
    ) -> list[Hit]:
        scored = [
            (dot(vector, v), chunk_id)
            for chunk_id, v in self._vectors.items()
            if self._within(chunk_id, titles, wiki, exclude_wikis, source_filter)
            and (fingerprint is None or self._fingerprints[chunk_id] == fingerprint)
        ]
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [Hit(self._chunks[chunk_id], score) for score, chunk_id in scored[:limit]]

    async def keyword_search(
        self,
        terms: Iterable[str],
        *,
        limit: int,
        max_df_ratio: float = 0.2,
        titles: Sequence[str] | None = None,
        wiki: str | None = None,
        exclude_wikis: Collection[str] | None = None,
        source_filter: SourceFilter | None = None,
    ) -> list[Hit]:
        weights = term_weights(terms, self._df, len(self._chunks), max_df_ratio=max_df_ratio)
        scored = []
        for chunk_id, have in self._terms.items():
            if not self._within(chunk_id, titles, wiki, exclude_wikis, source_filter):
                continue
            score = sum(weight for term, weight in weights.items() if term in have)
            if score > 0:
                scored.append((score, chunk_id))
        scored.sort(key=lambda item: (-item[0], item[1]))
        pool = [self._chunks[chunk_id] for _, chunk_id in scored[: max(limit, KEYWORD_POOL)]]
        average_chars = (await self.info()).average_chars
        return _best_by_bm25(pool, weights, average_chars, limit)

    async def recent_documents(
        self, query: NewsQuery, *, exclude_wikis: Collection[str] | None = None
    ) -> NewsResult:
        return select_recent_documents(self._chunks.values(), query, exclude_wikis=exclude_wikis)

    async def outline(
        self, chunk_ids: Collection[str], *, source_filter: SourceFilter | None = None
    ) -> list[ChunkPlace]:
        docs = {
            self._chunks[cid].doc_key
            for cid in chunk_ids
            if cid in self._chunks
            and (source_filter is None or source_filter.allows(self._chunks[cid]))
        }
        places = [
            ChunkPlace(c.chunk_id, c.doc_key, c.section_path, c.seq, len(c.text))
            for c in self._chunks.values()
            if c.doc_key in docs and (source_filter is None or source_filter.allows(c))
        ]
        return sorted(places, key=lambda p: (p.doc_key, p.seq, p.chunk_id))

    async def get(
        self, chunk_ids: Collection[str], *, source_filter: SourceFilter | None = None
    ) -> list[KbChunk]:
        return [
            self._chunks[cid]
            for cid in chunk_ids
            if cid in self._chunks
            and (source_filter is None or source_filter.allows(self._chunks[cid]))
        ]

    async def page(
        self, title: str, *, url_prefix: str = "", source_filter: SourceFilter | None = None
    ) -> list[KbChunk]:
        chunks = [
            c
            for c in self._chunks.values()
            if c.title == title
            and c.url.startswith(url_prefix)
            and (source_filter is None or source_filter.allows(c))
        ]
        return sorted(chunks, key=lambda c: (c.doc_key, c.seq, c.chunk_id))


_COLUMNS = (
    "c.chunk_id, c.doc_key, c.type, c.title, c.text, c.url, c.source, c.section_path, c.seq, "
    "c.date, c.date_inferred, c.entities, c.meta, c.wiki"
)

_UPSERT = """
INSERT INTO kb_chunks (
    chunk_id, doc_key, type, title, text, url, source, section_path, seq, date, date_inferred,
    entities, meta, terms, embedding, fingerprint, row_hash, wiki, updated_at
) VALUES (
    $1, $2, $3, $4, $5, $6, $7, $8::text[], $9, $10, $11,
    $12::jsonb, $13::jsonb, $14::text[], $15::text::vector, $16, $17, $18, now()
)
ON CONFLICT (chunk_id) DO UPDATE SET
    wiki = EXCLUDED.wiki, doc_key = EXCLUDED.doc_key, type = EXCLUDED.type, title = EXCLUDED.title,
    text = EXCLUDED.text, url = EXCLUDED.url, source = EXCLUDED.source,
    section_path = EXCLUDED.section_path, seq = EXCLUDED.seq, date = EXCLUDED.date,
    date_inferred = EXCLUDED.date_inferred, entities = EXCLUDED.entities, meta = EXCLUDED.meta,
    terms = EXCLUDED.terms, embedding = EXCLUDED.embedding, fingerprint = EXCLUDED.fingerprint,
    row_hash = EXCLUDED.row_hash, updated_at = now()
"""

# 临时表只存本批文档，向量随有限 batch 流入 PG，不在 Python 汇集全部向量。
_STAGE_DOCUMENT = _UPSERT.partition("ON CONFLICT")[0].replace(
    "INSERT INTO kb_chunks", "INSERT INTO kb_sync_documents"
)
_PUBLISH_DOCUMENTS = (
    "INSERT INTO kb_chunks SELECT * FROM kb_sync_documents "
    "WHERE wiki = $1 AND doc_key = ANY($2::text[]) ON CONFLICT"
    + _UPSERT.partition("ON CONFLICT")[2]
    + " WHERE kb_chunks.wiki = $1 AND kb_chunks.doc_key = ANY($2::text[])"
    " AND kb_chunks.row_hash IS DISTINCT FROM EXCLUDED.row_hash"
)


def _record_values(
    chunk: KbChunk, vector: Sequence[float] | None, fingerprint: str
) -> tuple[Any, ...]:
    return (
        chunk.chunk_id,
        chunk.doc_key,
        chunk.kind,
        chunk.title,
        chunk.text,
        chunk.url,
        chunk.source,
        list(chunk.section_path),
        chunk.seq,
        chunk.date,
        chunk.date_inferred,
        json.dumps(chunk.entities, ensure_ascii=False),
        json.dumps(chunk.meta, ensure_ascii=False),
        sorted(extract_terms(chunk.text)),
        vector_literal(vector) if vector is not None else None,
        fingerprint if vector is not None else None,
        chunk.row_hash(fingerprint if vector is not None else None),
        chunk.wiki,
    )


# 查询向量先在 q 里算好一次：直接把 $1::text::vector 写在 ORDER BY 里，
# 预备语句换成通用计划后会每一行都重新解析一遍这几万个字符
_VECTOR_SEARCH = f"""
WITH q AS MATERIALIZED (SELECT $1::text::vector AS v)
SELECT {_COLUMNS}, -(c.embedding <#> q.v) AS score
FROM kb_chunks c, q
WHERE c.embedding IS NOT NULL AND ($3::text[] IS NULL OR c.title = ANY($3::text[]))
  AND ($4::text IS NULL OR c.wiki = $4) AND ($5::text IS NULL OR c.fingerprint = $5)
  AND ($6::text[] IS NULL OR c.wiki <> ALL($6::text[]))
  AND /*SOURCE_FILTER*/ TRUE
ORDER BY c.embedding <#> q.v, c.chunk_id
LIMIT $2
"""

_KEYWORD_SEARCH = f"""
WITH q AS MATERIALIZED (
    SELECT term, weight FROM unnest($1::text[], $2::float8[]) AS t(term, weight)
),
scored AS (
    SELECT k.chunk_id, sum(q.weight) AS score
    FROM q JOIN kb_chunks k ON k.terms @> ARRAY[q.term]
    WHERE ($4::text[] IS NULL OR k.title = ANY($4::text[])) AND ($5::text IS NULL OR k.wiki = $5)
      AND ($6::text[] IS NULL OR k.wiki <> ALL($6::text[]))
      AND /*SOURCE_FILTER*/ TRUE
    GROUP BY k.chunk_id
    ORDER BY score DESC, k.chunk_id
    LIMIT $3
)
SELECT {_COLUMNS}, s.score
FROM scored s JOIN kb_chunks c USING (chunk_id)
ORDER BY s.score DESC, c.chunk_id
"""

# PostgreSQL 17：先在库内按完整文档筛选、统计、限篇数，最后只传每篇的首片。
# 历史 date 是文本；形状与有效性都过关才参与 ISO 日期比较，绝不拿 updated_at 补。
# 主题用切片×去重词集合聚合覆盖篇目；禁止按每篇相关子查询重扫全部 candidates。
# 文档聚合同时记首片主键；仅为selected原帖收集captures，最后按主键补正文。
# 即使新库未ANALYZE、CTE被估成1行，也不关联两份完整文档集造成N²扫描。
_RECENT_DOCUMENTS = f"""
WITH candidates AS MATERIALIZED (
    SELECT {_COLUMNS},
           coalesce('canonical:' || ({canonical_sql("c")}), 'doc:' || c.doc_key) AS identity,
           CASE WHEN c.date ~ '^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}$'
                     AND pg_input_is_valid(c.date, 'date')
                THEN c.date ELSE NULL END AS published_date,
           CASE WHEN c.meta->>'published_at' ~ '^{PUBLISHED_AT_PATTERN}$'
                     AND pg_input_is_valid(c.meta->>'published_at', 'timestamptz')
                THEN (c.meta->>'published_at')::timestamptz ELSE NULL END AS published_at
    FROM kb_chunks c
    WHERE c.type IN ('announcement', 'post')
      AND ($1::text[] IS NULL OR c.meta->>'source_platform' = ANY($1::text[]))
      AND ($2::text[] IS NULL OR c.wiki <> ALL($2::text[]))
      AND /*SOURCE_FILTER*/ TRUE
),
documents AS MATERIALIZED (
    SELECT doc_key, min(identity) AS identity, max(published_date) AS published_date,
           max(published_at) AS published_at,
           (array_agg(chunk_id ORDER BY seq, chunk_id))[1] AS head_chunk_id
    FROM candidates GROUP BY doc_key
),
canonical_coverage AS (
    SELECT identity, max(published_date) AS published_date FROM documents GROUP BY identity
),
coverage AS (
    SELECT max(published_date) FILTER (WHERE published_date <= $3::text) AS source_latest_date,
           count(*) FILTER (WHERE published_date IS NULL) AS undated_documents
    FROM canonical_coverage
),
query_words AS MATERIALIZED (
    SELECT DISTINCT lower(word) AS word FROM unnest($6::text[]) AS q(word)
),
topic_documents AS MATERIALIZED (
    SELECT p.doc_key FROM candidates p JOIN query_words q
      ON strpos(lower(p.title), q.word) > 0 OR strpos(lower(p.text), q.word) > 0
    GROUP BY p.doc_key
    HAVING count(DISTINCT q.word) = (SELECT count(*) FROM query_words)
),
matching_copies AS MATERIALIZED (
    SELECT d.doc_key, d.identity, d.published_date, d.published_at, d.head_chunk_id
    FROM documents d
    WHERE d.published_date <= $3::text
      AND ($4::text IS NULL OR d.published_date >= $4::text)
      AND ($5::text IS NULL OR d.published_date <= $5::text)
      AND (NOT EXISTS (SELECT 1 FROM query_words)
           OR d.doc_key IN (SELECT doc_key FROM topic_documents))
),
matching AS MATERIALIZED (
    SELECT DISTINCT ON (identity) doc_key, identity, published_date, published_at, head_chunk_id
    FROM matching_copies
    ORDER BY identity, published_date DESC, published_at DESC NULLS LAST, doc_key
),
selected AS MATERIALIZED (
    SELECT doc_key, identity, published_date, published_at, head_chunk_id FROM matching
    ORDER BY published_date DESC, published_at DESC NULLS LAST, doc_key LIMIT $7
),
selected_copies AS MATERIALIZED (
    SELECT m.* FROM matching_copies m WHERE m.identity IN (SELECT identity FROM selected)
),
captures AS MATERIALIZED (
    SELECT m.identity, count(*) AS copies,
           jsonb_agg(jsonb_build_object('source_id', h.wiki, 'doc_key', h.doc_key, 'url', h.url)
                     ORDER BY h.doc_key) AS captures
    FROM selected_copies m JOIN kb_chunks h ON h.chunk_id = m.head_chunk_id GROUP BY m.identity
)
SELECT c.chunk_id, c.doc_key, c.type, c.title, c.text, c.url, c.source, c.section_path, c.seq,
       s.published_date AS date, c.date_inferred, c.entities,
       CASE WHEN cp.copies > 1 THEN jsonb_set(c.meta, '{{captures}}', cp.captures)
            ELSE c.meta END AS meta, c.wiki,
       coverage.source_latest_date, coverage.undated_documents,
       (SELECT count(*) FROM matching) AS matching_documents
FROM coverage LEFT JOIN selected s ON TRUE
LEFT JOIN kb_chunks c ON c.chunk_id = s.head_chunk_id
LEFT JOIN captures cp ON cp.identity = s.identity
ORDER BY s.published_date DESC, s.published_at DESC NULLS LAST, s.doc_key, c.chunk_id
"""

_OUTLINE = """
SELECT c.chunk_id, c.doc_key, c.section_path, c.seq, length(c.text) AS chars
FROM kb_chunks c
WHERE c.doc_key IN (SELECT doc_key FROM kb_chunks WHERE chunk_id = ANY($1::text[]))
ORDER BY c.doc_key, c.seq, c.chunk_id
"""

_PAGE = f"""
SELECT {_COLUMNS}
FROM kb_chunks c
WHERE c.title = $1 AND starts_with(c.url, $2)
ORDER BY c.doc_key, c.seq, c.chunk_id
"""


def vector_literal(vector: Sequence[float]) -> str:
    """pgvector 的文本写法。9 位有效数字足够还原单精度。"""
    return "[" + ",".join(f"{x:.9g}" for x in vector) + "]"


def _chunk(row: Any) -> KbChunk:
    return KbChunk(
        chunk_id=row["chunk_id"],
        doc_key=row["doc_key"],
        kind=row["type"],
        title=row["title"],
        text=row["text"],
        url=row["url"],
        source=row["source"],
        section_path=tuple(row["section_path"]),
        seq=row["seq"],
        date=row["date"],
        date_inferred=row["date_inferred"],
        entities=json.loads(row["entities"]),
        meta=json.loads(row["meta"]),
        wiki=row["wiki"],
    )


def _info(stats: dict[str, Any]) -> StoreInfo:
    return StoreInfo(
        chunks=int(stats.get("chunks") or 0),
        with_vectors=int(stats.get("with_vectors") or 0),
        fingerprint=stats.get("fingerprint"),
        loaded_at=stats.get("loaded_at"),
        average_chars=float(stats.get("average_chars") or 0),
    )


async def _recount_terms(conn: Any) -> int:
    """每个词出现在多少个切片里：整表重算（各个 wiki 的切片一起算）。返回一共多少个词。"""
    await conn.execute("TRUNCATE kb_terms")
    await conn.execute(
        "INSERT INTO kb_terms (term, df) "
        "SELECT term, count(*) FROM kb_chunks, unnest(terms) AS term GROUP BY term"
    )
    return int(await conn.fetchval("SELECT count(*) FROM kb_terms"))


class PostgresKnowledgeStore:
    """落在 ``kb_chunks`` / ``kb_terms`` / ``kb_meta``（见 migrations/0003_knowledge.sql）。"""

    def __init__(self, db: Database):
        self._db = db

    async def sync(
        self,
        records: Iterable[Record],
        *,
        fingerprint: str,
        wiki: str = "",
        replace_unlabelled: bool = False,
    ) -> SyncReport:
        started = time.perf_counter()
        report = SyncReport()
        mine = sorted({wiki, ""} if replace_unlabelled else {wiki})
        async with self._db.pool.acquire() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock($1)", _TERMS_WRITE_LOCK)
            rows = await conn.fetch(
                "SELECT chunk_id, row_hash FROM kb_chunks WHERE wiki = ANY($1::text[])", mine
            )
            existing = {row["chunk_id"]: row["row_hash"] for row in rows}
            seen: set[str] = set()
            batch: list[tuple[Any, ...]] = []
            for chunk, vector in records:
                _check_namespace(chunk, wiki)
                report.total += 1
                seen.add(chunk.chunk_id)
                if vector is not None:
                    report.with_vectors += 1
                row_hash = chunk.row_hash(fingerprint if vector is not None else None)
                if existing.get(chunk.chunk_id) == row_hash:
                    report.unchanged += 1
                    continue
                batch.append(
                    (
                        chunk.chunk_id,
                        chunk.doc_key,
                        chunk.kind,
                        chunk.title,
                        chunk.text,
                        chunk.url,
                        chunk.source,
                        list(chunk.section_path),
                        chunk.seq,
                        chunk.date,
                        chunk.date_inferred,
                        json.dumps(chunk.entities, ensure_ascii=False),
                        json.dumps(chunk.meta, ensure_ascii=False),
                        sorted(extract_terms(chunk.text)),
                        vector_literal(vector) if vector is not None else None,
                        fingerprint if vector is not None else None,
                        row_hash,
                        wiki,
                    )
                )
                if len(batch) >= _UPSERT_BATCH:
                    await conn.executemany(_UPSERT, batch)
                    report.upserted += len(batch)
                    batch.clear()
            if batch:
                await conn.executemany(_UPSERT, batch)
                report.upserted += len(batch)
            gone = [chunk_id for chunk_id in existing if chunk_id not in seen]
            if gone:
                await conn.execute("DELETE FROM kb_chunks WHERE chunk_id = ANY($1::text[])", gone)
            report.deleted = len(gone)
            stale = [meta_key(STATS_KEY, other) for other in mine if other != wiki]
            await conn.execute("DELETE FROM kb_meta WHERE key = ANY($1::text[])", stale)

            report.terms = await _recount_terms(conn)
            average_chars = await conn.fetchval(
                "SELECT avg(length(text)) FROM kb_chunks WHERE wiki = $1", wiki
            )
            stats = {
                "chunks": report.total,
                "with_vectors": report.with_vectors,
                "fingerprint": fingerprint if report.with_vectors else None,
                "loaded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "average_chars": float(average_chars or 0),
            }
            await conn.execute(
                "INSERT INTO kb_meta (key, value) VALUES ($1, $2::jsonb) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                meta_key(STATS_KEY, wiki),
                json.dumps(stats),
            )
        # 大批量写入后更新统计信息，查询计划才靠谱
        await self._db.pool.execute("ANALYZE kb_chunks")
        await self._db.pool.execute("ANALYZE kb_terms")
        report.seconds = time.perf_counter() - started
        return report

    async def sync_documents(
        self,
        records: Iterable[Record],
        *,
        fingerprint: str,
        wiki: str,
        doc_keys: Sequence[str],
    ) -> SyncReport:
        started = time.perf_counter()
        scope = sorted(_document_scope(wiki, doc_keys))
        report = SyncReport()
        async with self._db.pool.acquire() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock($1)", _TERMS_WRITE_LOCK)
            await conn.execute(
                "CREATE TEMP TABLE kb_sync_documents (LIKE kb_chunks INCLUDING DEFAULTS, "
                "PRIMARY KEY (chunk_id)) ON COMMIT DROP"
            )
            batch: list[tuple[Any, ...]] = []
            batch_ids: set[str] = set()

            async def flush() -> None:
                if not batch:
                    return
                if await conn.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM kb_sync_documents "
                    "WHERE chunk_id = ANY($1::text[]))",
                    list(batch_ids),
                ):
                    raise ValueError("同批切片主键重复")
                await conn.executemany(_STAGE_DOCUMENT, batch)
                batch.clear()
                batch_ids.clear()

            scope_set = set(scope)
            for chunk, vector in records:
                _check_namespace(chunk, wiki)
                if chunk.doc_key not in scope_set or chunk.chunk_id == f"{wiki}:":
                    raise ValueError("切片不属于指定文档作用域")
                if chunk.chunk_id in batch_ids:
                    raise ValueError("同批切片主键重复")
                batch.append(_record_values(chunk, vector, fingerprint))
                batch_ids.add(chunk.chunk_id)
                report.total += 1
                report.with_vectors += vector is not None
                if len(batch) >= _UPSERT_BATCH:
                    await flush()
            await flush()
            if await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM kb_chunks c JOIN kb_sync_documents s "
                "USING (chunk_id) WHERE c.wiki <> $1 OR NOT (c.doc_key = ANY($2::text[])))",
                wiki,
                scope,
            ):
                raise ValueError("切片主键属于作用域外文档或来源")
            report.unchanged = int(
                await conn.fetchval(
                    "SELECT count(*) FROM kb_sync_documents s JOIN kb_chunks c USING (chunk_id) "
                    "WHERE c.wiki = $1 AND c.doc_key = ANY($2::text[]) AND c.row_hash = s.row_hash",
                    wiki,
                    scope,
                )
            )
            report.upserted = report.total - report.unchanged
            # DF 是每个词所在切片数，不是词出现次数；先聚合本作用域前后差量。
            # 不读旧向量、不 TRUNCATE 全库；与 full sync/keep_only 共用事务锁。
            await conn.execute(
                "CREATE TEMP TABLE kb_sync_term_delta ON COMMIT DROP AS "
                "SELECT term, sum(delta)::int AS delta FROM ("
                " SELECT term, -1 AS delta FROM kb_chunks c, unnest(c.terms) AS term"
                " WHERE c.wiki = $1 AND c.doc_key = ANY($2::text[])"
                " UNION ALL SELECT term, 1 AS delta FROM kb_sync_documents, unnest(terms) AS term"
                ") changes GROUP BY term HAVING sum(delta) <> 0",
                wiki,
                scope,
            )
            deleted = await conn.execute(
                "DELETE FROM kb_chunks c WHERE c.wiki = $1 AND c.doc_key = ANY($2::text[]) "
                "AND NOT EXISTS (SELECT 1 FROM kb_sync_documents s WHERE s.chunk_id = c.chunk_id)",
                wiki,
                scope,
            )
            report.deleted = int(deleted.rpartition(" ")[2])
            await conn.execute(_PUBLISH_DOCUMENTS, wiki, scope)
            await conn.execute(
                "INSERT INTO kb_terms (term, df) SELECT term, delta FROM kb_sync_term_delta "
                "ON CONFLICT (term) DO UPDATE SET df = kb_terms.df + EXCLUDED.df"
            )
            if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM kb_terms WHERE df < 0)"):
                raise ValueError("关键词词频统计不一致，事务已取消")
            await conn.execute("DELETE FROM kb_terms WHERE df = 0")
            report.terms = int(await conn.fetchval("SELECT count(*) FROM kb_terms"))
            if scope:
                stats_row = await conn.fetchrow(
                    "SELECT count(*) AS chunks, count(embedding) AS with_vectors, "
                    "CASE WHEN count(DISTINCT fingerprint) "
                    "FILTER (WHERE embedding IS NOT NULL) = 1 "
                    "THEN min(fingerprint) FILTER (WHERE embedding IS NOT NULL) "
                    "END AS fingerprint, "
                    "coalesce(avg(length(text)), 0) AS average_chars "
                    "FROM kb_chunks WHERE wiki = $1",
                    wiki,
                )
                stats = dict(stats_row)
                stats["average_chars"] = float(stats["average_chars"])
                stats["loaded_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
                await conn.execute(
                    "INSERT INTO kb_meta (key, value) VALUES ($1, $2::jsonb) "
                    "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                    meta_key(STATS_KEY, wiki),
                    json.dumps(stats),
                )
        report.seconds = time.perf_counter() - started
        return report

    async def resource_usage(self, source_ids: Collection[str]) -> dict[str, int]:
        """只读物理快照；全库和 WAL 的增量包含其他来源，故只可作保守预算。"""
        if isinstance(source_ids, (str, bytes)):
            raise ValueError("source_ids 必须是来源集合")
        sources = sorted(set(source_ids))
        for source_id in sources:
            _document_scope(source_id, ())
        row = await self._db.pool.fetchrow(
            "SELECT coalesce(sum(pg_column_size(c)), 0)::bigint AS source_rows_bytes, "
            "pg_database_size(current_database())::bigint AS database_bytes, "
            "pg_wal_lsn_diff(pg_current_wal_lsn(), '0/0'::pg_lsn)::bigint "
            "AS wal_position_bytes, "
            "(extract(epoch from pg_postmaster_start_time()) * 1000000)::bigint AS pg_started_at "
            "FROM kb_chunks c WHERE c.wiki = ANY($1::text[])",
            sources,
        )
        # 权限/连接/恢复模式等错误由 PG 原样抛出，不能用 0 掩盖测量失败。
        return {
            key: int(row[key])
            for key in (
                "source_rows_bytes",
                "database_bytes",
                "wal_position_bytes",
                "pg_started_at",
            )
        }

    async def info(self) -> StoreInfo:
        prefix = meta_key(STATS_KEY, "")
        rows = await self._db.pool.fetch(
            "SELECT key, value FROM kb_meta WHERE starts_with(key, $1) ORDER BY key", prefix
        )
        return combined_info(
            {row["key"].removeprefix(prefix): _info(json.loads(row["value"])) for row in rows}
        )

    async def keep_only(self, wikis: Collection[str]) -> int:
        async with self._db.pool.acquire() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock($1)", _TERMS_WRITE_LOCK)
            status = await conn.execute(
                "DELETE FROM kb_chunks WHERE wiki <> '' AND wiki <> ALL($1::text[])", list(wikis)
            )
            deleted = int(status.rpartition(" ")[2])
            keys = [meta_key(STATS_KEY, wiki) for wiki in ("", *wikis)]
            await conn.execute(
                "DELETE FROM kb_meta WHERE starts_with(key, $1) AND key <> ALL($2::text[])",
                meta_key(STATS_KEY, ""),
                keys,
            )
            if deleted:
                await _recount_terms(conn)
        return deleted

    async def vector_search(
        self,
        vector: Sequence[float],
        *,
        limit: int,
        titles: Sequence[str] | None = None,
        wiki: str | None = None,
        fingerprint: str | None = None,
        exclude_wikis: Collection[str] | None = None,
        source_filter: SourceFilter | None = None,
    ) -> list[Hit]:
        condition, args = (source_filter or SourceFilter()).sql("c", 7)
        rows = await self._db.pool.fetch(
            _VECTOR_SEARCH.replace("/*SOURCE_FILTER*/ TRUE", f"({condition})"),
            vector_literal(vector),
            limit,
            list(titles) if titles else None,
            wiki,
            fingerprint,
            list(exclude_wikis) if exclude_wikis else None,
            *args,
        )
        return [Hit(_chunk(row), float(row["score"])) for row in rows]

    async def keyword_search(
        self,
        terms: Iterable[str],
        *,
        limit: int,
        max_df_ratio: float = 0.2,
        titles: Sequence[str] | None = None,
        wiki: str | None = None,
        exclude_wikis: Collection[str] | None = None,
        source_filter: SourceFilter | None = None,
    ) -> list[Hit]:
        wanted = sorted(set(terms))
        if not wanted:
            return []
        pool = self._db.pool
        rows = await pool.fetch(
            "SELECT term, df FROM kb_terms WHERE term = ANY($1::text[])", wanted
        )
        info = await self.info()
        weights = term_weights(
            wanted, {row["term"]: row["df"] for row in rows}, info.chunks, max_df_ratio=max_df_ratio
        )
        if not weights:
            return []
        condition, args = (source_filter or SourceFilter()).sql("k", 7)
        rows = await pool.fetch(
            _KEYWORD_SEARCH.replace("/*SOURCE_FILTER*/ TRUE", f"({condition})"),
            list(weights),
            [float(w) for w in weights.values()],
            max(limit, KEYWORD_POOL),
            list(titles) if titles else None,
            wiki,
            list(exclude_wikis) if exclude_wikis else None,
            *args,
        )
        return _best_by_bm25((_chunk(row) for row in rows), weights, info.average_chars, limit)

    async def recent_documents(
        self, query: NewsQuery, *, exclude_wikis: Collection[str] | None = None
    ) -> NewsResult:
        if query.source_platforms == ():
            return NewsResult(chunks=[])
        condition, args = (
            SourceFilter(
                source_ids=query.source_ids,
                author_ids=query.author_ids,
            )
            .intersect(query.source_filter)
            .sql("c", 8)
        )
        rows = await self._db.pool.fetch(
            _RECENT_DOCUMENTS.replace("/*SOURCE_FILTER*/ TRUE", f"({condition})"),
            list(query.source_platforms) if query.source_platforms is not None else None,
            list(exclude_wikis) if exclude_wikis else None,
            query.as_of or date.today().isoformat(),
            query.date_from,
            query.date_to,
            list(query.keywords),
            query.limit,
            *args,
        )
        return NewsResult(
            chunks=[_chunk(row) for row in rows if row["chunk_id"] is not None],
            source_latest_date=rows[0]["source_latest_date"],
            matching_documents=int(rows[0]["matching_documents"]),
            undated_documents=int(rows[0]["undated_documents"]),
        )

    async def outline(
        self, chunk_ids: Collection[str], *, source_filter: SourceFilter | None = None
    ) -> list[ChunkPlace]:
        if not chunk_ids:
            return []
        condition, args = (source_filter or SourceFilter()).sql("c", 2)
        sql = _OUTLINE.replace(
            "FROM kb_chunks WHERE chunk_id = ANY($1::text[])",
            f"FROM kb_chunks c WHERE chunk_id = ANY($1::text[]) AND ({condition})",
        ).replace("ORDER BY c.doc_key", f"AND ({condition}) ORDER BY c.doc_key")
        rows = await self._db.pool.fetch(sql, list(chunk_ids), *args)
        return [
            ChunkPlace(
                chunk_id=row["chunk_id"],
                doc_key=row["doc_key"],
                section_path=tuple(row["section_path"]),
                seq=row["seq"],
                chars=row["chars"],
            )
            for row in rows
        ]

    async def get(
        self, chunk_ids: Collection[str], *, source_filter: SourceFilter | None = None
    ) -> list[KbChunk]:
        if not chunk_ids:
            return []
        condition, args = (source_filter or SourceFilter()).sql("c", 2)
        rows = await self._db.pool.fetch(
            f"SELECT {_COLUMNS} FROM kb_chunks c "
            f"WHERE c.chunk_id = ANY($1::text[]) AND ({condition})",
            list(chunk_ids),
            *args,
        )
        return [_chunk(row) for row in rows]

    async def page(
        self, title: str, *, url_prefix: str = "", source_filter: SourceFilter | None = None
    ) -> list[KbChunk]:
        condition, args = (source_filter or SourceFilter()).sql("c", 3)
        sql = _PAGE.replace("ORDER BY", f"AND ({condition}) ORDER BY")
        rows = await self._db.pool.fetch(sql, title, url_prefix, *args)
        return [_chunk(row) for row in rows]
