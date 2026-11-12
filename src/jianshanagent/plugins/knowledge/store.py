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
from typing import Any, Protocol

from jianshanagent.core.db import Database
from jianshanagent.core.embedding import dot
from jianshanagent.plugins.knowledge.terms import bm25_score, extract_terms, term_weights

#: （切片，向量或 None）。没有向量的切片只能被关键词查到
Record = tuple["KbChunk", Sequence[float] | None]

#: ``kb_meta`` 表里的键：一个 wiki 的概况是 ``stats:<wiki>``，
#: 自动更新的状态是 ``sync:<wiki>``（sync.py 写，管理后台读）
STATS_KEY = "stats"
SYNC_STATUS_KEY = "sync"
_UPSERT_BATCH = 100
#: 关键词检索粗选多少片来细算
KEYWORD_POOL = 100


def scoped(wiki: str, local_id: str) -> str:
    """库里的 id：``<wiki>:<切片文件里的 id>``。没说是哪个 wiki（测试里）就是文件里的 id。"""
    return f"{wiki}:{local_id}" if wiki else local_id


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

    async def info(self) -> StoreInfo:
        """整库的概况；各个 wiki 的在 ``wikis`` 里。"""

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
    ) -> list[Hit]:
        """按向量相似度从高到低。``titles``：只在这些页面的切片里找；``wiki``：只在这个 wiki 里找。

        ``fingerprint``：只和用这个指纹算的向量比（各个 wiki 的向量可能不是同一批算的）。
        """

    async def keyword_search(
        self,
        terms: Iterable[str],
        *,
        limit: int,
        max_df_ratio: float = 0.2,
        titles: Sequence[str] | None = None,
        wiki: str | None = None,
    ) -> list[Hit]:
        """按关键词得分从高到低；一个查询词都不含的切片不返回。``titles``、``wiki`` 同上。"""

    async def outline(self, chunk_ids: Collection[str]) -> list[ChunkPlace]:
        """这些切片所在的页面的全部切片（不带正文），按页面、页内先后排。库里没有的 id 不管。"""

    async def get(self, chunk_ids: Collection[str]) -> list[KbChunk]:
        """按 id 取切片；库里没有的不返回。"""

    async def page(self, title: str, *, url_prefix: str = "") -> list[KbChunk]:
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

    def _within(self, chunk_id: str, titles: Sequence[str] | None, wiki: str | None) -> bool:
        chunk = self._chunks[chunk_id]
        return (titles is None or chunk.title in titles) and (wiki is None or chunk.wiki == wiki)

    async def vector_search(
        self,
        vector: Sequence[float],
        *,
        limit: int,
        titles: Sequence[str] | None = None,
        wiki: str | None = None,
        fingerprint: str | None = None,
    ) -> list[Hit]:
        scored = [
            (dot(vector, v), chunk_id)
            for chunk_id, v in self._vectors.items()
            if self._within(chunk_id, titles, wiki)
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
    ) -> list[Hit]:
        weights = term_weights(terms, self._df, len(self._chunks), max_df_ratio=max_df_ratio)
        scored = []
        for chunk_id, have in self._terms.items():
            if not self._within(chunk_id, titles, wiki):
                continue
            score = sum(weight for term, weight in weights.items() if term in have)
            if score > 0:
                scored.append((score, chunk_id))
        scored.sort(key=lambda item: (-item[0], item[1]))
        pool = [self._chunks[chunk_id] for _, chunk_id in scored[: max(limit, KEYWORD_POOL)]]
        average_chars = (await self.info()).average_chars
        return _best_by_bm25(pool, weights, average_chars, limit)

    async def outline(self, chunk_ids: Collection[str]) -> list[ChunkPlace]:
        docs = {self._chunks[cid].doc_key for cid in chunk_ids if cid in self._chunks}
        places = [
            ChunkPlace(c.chunk_id, c.doc_key, c.section_path, c.seq, len(c.text))
            for c in self._chunks.values()
            if c.doc_key in docs
        ]
        return sorted(places, key=lambda p: (p.doc_key, p.seq, p.chunk_id))

    async def get(self, chunk_ids: Collection[str]) -> list[KbChunk]:
        return [self._chunks[cid] for cid in chunk_ids if cid in self._chunks]

    async def page(self, title: str, *, url_prefix: str = "") -> list[KbChunk]:
        chunks = [
            c for c in self._chunks.values() if c.title == title and c.url.startswith(url_prefix)
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

# 查询向量先在 q 里算好一次：直接把 $1::text::vector 写在 ORDER BY 里，
# 预备语句换成通用计划后会每一行都重新解析一遍这几万个字符
_VECTOR_SEARCH = f"""
WITH q AS MATERIALIZED (SELECT $1::text::vector AS v)
SELECT {_COLUMNS}, -(c.embedding <#> q.v) AS score
FROM kb_chunks c, q
WHERE c.embedding IS NOT NULL AND ($3::text[] IS NULL OR c.title = ANY($3::text[]))
  AND ($4::text IS NULL OR c.wiki = $4) AND ($5::text IS NULL OR c.fingerprint = $5)
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
    GROUP BY k.chunk_id
    ORDER BY score DESC, k.chunk_id
    LIMIT $3
)
SELECT {_COLUMNS}, s.score
FROM scored s JOIN kb_chunks c USING (chunk_id)
ORDER BY s.score DESC, c.chunk_id
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
            rows = await conn.fetch(
                "SELECT chunk_id, row_hash FROM kb_chunks WHERE wiki = ANY($1::text[])", mine
            )
            existing = {row["chunk_id"]: row["row_hash"] for row in rows}
            seen: set[str] = set()
            batch: list[tuple[Any, ...]] = []
            for chunk, vector in records:
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
    ) -> list[Hit]:
        rows = await self._db.pool.fetch(
            _VECTOR_SEARCH,
            vector_literal(vector),
            limit,
            list(titles) if titles else None,
            wiki,
            fingerprint,
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
        rows = await pool.fetch(
            _KEYWORD_SEARCH,
            list(weights),
            [float(w) for w in weights.values()],
            max(limit, KEYWORD_POOL),
            list(titles) if titles else None,
            wiki,
        )
        return _best_by_bm25((_chunk(row) for row in rows), weights, info.average_chars, limit)

    async def outline(self, chunk_ids: Collection[str]) -> list[ChunkPlace]:
        if not chunk_ids:
            return []
        rows = await self._db.pool.fetch(_OUTLINE, list(chunk_ids))
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

    async def get(self, chunk_ids: Collection[str]) -> list[KbChunk]:
        if not chunk_ids:
            return []
        rows = await self._db.pool.fetch(
            f"SELECT {_COLUMNS} FROM kb_chunks c WHERE c.chunk_id = ANY($1::text[])",
            list(chunk_ids),
        )
        return [_chunk(row) for row in rows]

    async def page(self, title: str, *, url_prefix: str = "") -> list[KbChunk]:
        rows = await self._db.pool.fetch(_PAGE, title, url_prefix)
        return [_chunk(row) for row in rows]
