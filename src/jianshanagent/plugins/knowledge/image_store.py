"""wiki 图片索引的存储和按向量找图。

线上用 Postgres（表见 migrations/0004_kb_images.sql），向量放 pgvector；
没有数据库时（测试）用进程内的实现，两者行为一致。做法照文本知识库的 store.py：

- 库里的向量都已归一化，按内积排，内积就是余弦相似度。几千张图，不建索引、整表精确比，不会漏。
- 同一个造型常有好几张图（立绘大图、卡面小图……），它们的 ``subject`` 相同：
  找图的结果里每个 ``subject`` 只留最像的一张，免得同一个皮肤占掉好几个名额。
- 每行记着向量是怎么来的：``fingerprint``（提供方 + 型号 + 维度，和文本知识库的一样）
  和 ``preprocess``（算向量前对图片做的预处理）。查询图片这两样都和库里的一致，相似度才有意义。

导入（``sync``）按整库对齐：传进来的是当前全部图片，库里多出来的删掉，内容没变的不动。
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from jianshanagent.core.db import Database
from jianshanagent.core.embedding import dot
from jianshanagent.plugins.knowledge.store import vector_literal

#: （图片，向量或 None）。没有向量的图片找不到，入库只是为了看得见「还缺多少」
ImageRecord = tuple["KbImage", Sequence[float] | None]

_UPSERT_BATCH = 100


@dataclass(frozen=True, slots=True)
class KbImage:
    #: wiki 上的文件名，稳定
    image_id: str
    #: 式神立绘 / 皮肤立绘 / 式神头像 / 御魂图标 ……（中文，直接给人看）
    kind: str
    #: 给人看的一句话：不知火的皮肤「蝶步韶华」
    label: str
    page_title: str = ""
    url: str = ""
    image_url: str = ""
    #: 给大模型当资料的说明：这是谁的什么图、皮肤名、获取方式……
    text: str = ""
    #: 画的是什么。同一个造型的几张图写同一个值，找图的结果里每个只留最像的一张
    subject: str = ""
    meta: dict[str, Any] = field(default_factory=dict)
    #: 原图内容的哈希（wiki 给的）：wiki 上换了图它就变，向量也跟着重算
    sha1: str = ""

    @property
    def group(self) -> str:
        """找图的结果按它去重；没写 ``subject`` 的图自己算一个。"""
        return self.subject or self.image_id

    @classmethod
    def from_json(cls, row: dict[str, Any]) -> KbImage:
        """``images.jsonl`` 的一行。"""
        return cls(
            image_id=str(row["image_id"]),
            kind=str(row["kind"]),
            label=str(row["label"]),
            page_title=str(row.get("page_title") or ""),
            url=str(row.get("url") or ""),
            image_url=str(row.get("image_url") or ""),
            text=str(row.get("text") or ""),
            subject=str(row.get("subject") or ""),
            meta=dict(row.get("meta") or {}),
            sha1=str(row.get("sha1") or ""),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "image_id": self.image_id,
            "kind": self.kind,
            "label": self.label,
            "subject": self.subject,
            "page_title": self.page_title,
            "url": self.url,
            "image_url": self.image_url,
            "text": self.text,
            "meta": self.meta,
            "sha1": self.sha1,
        }

    def row_hash(self, fingerprint: str | None, preprocess: str | None) -> str:
        """这一行存进库里的全部内容的哈希（没有向量时两个参数都传 None）。

        向量本身不参与：它由原图（``sha1``）、指纹和预处理决定，这三样都在里面了。
        """
        payload = [self.to_json(), fingerprint, preprocess]
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ImageMatch:
    image: KbImage
    #: 和查询向量的余弦相似度
    score: float


@dataclass(frozen=True, slots=True)
class ImageStoreInfo:
    images: int = 0
    with_vectors: int = 0
    #: 库里向量的指纹和预处理；没有向量时为 None
    fingerprint: str | None = None
    preprocess: str | None = None
    #: 最近一次有改动的导入时间
    loaded_at: str | None = None


@dataclass(slots=True)
class ImageSyncReport:
    total: int = 0
    upserted: int = 0
    unchanged: int = 0
    deleted: int = 0
    with_vectors: int = 0
    seconds: float = 0.0


class ImageStore(Protocol):
    async def sync(
        self, records: Iterable[ImageRecord], *, fingerprint: str, preprocess: str
    ) -> ImageSyncReport:
        """把库对齐成 ``records``（当前全部图片）。后两个参数说明这批向量是怎么来的。"""

    async def info(self) -> ImageStoreInfo: ...

    async def search(self, vector: Sequence[float], *, limit: int) -> list[ImageMatch]:
        """按相似度从高到低，最多 ``limit`` 张；每个 ``subject`` 只留最像的一张。"""


class MemoryImageStore:
    """进程内实现：测试用。"""

    def __init__(self) -> None:
        self._images: dict[str, KbImage] = {}
        self._hashes: dict[str, str] = {}
        self._vectors: dict[str, Sequence[float]] = {}
        self._info = ImageStoreInfo()

    async def sync(
        self, records: Iterable[ImageRecord], *, fingerprint: str, preprocess: str
    ) -> ImageSyncReport:
        started = time.perf_counter()
        report = ImageSyncReport()
        images: dict[str, KbImage] = {}
        hashes: dict[str, str] = {}
        vectors: dict[str, Sequence[float]] = {}
        for image, vector in records:
            report.total += 1
            images[image.image_id] = image
            if vector is not None:
                vectors[image.image_id] = vector
            how = (fingerprint, preprocess) if vector is not None else (None, None)
            row_hash = image.row_hash(*how)
            hashes[image.image_id] = row_hash
            if self._hashes.get(image.image_id) == row_hash:
                report.unchanged += 1
            else:
                report.upserted += 1
        report.deleted = len(set(self._images) - set(images))
        self._images, self._hashes, self._vectors = images, hashes, vectors
        report.with_vectors = len(vectors)
        if report.upserted or report.deleted or self._info.loaded_at is None:
            loaded_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        else:
            loaded_at = self._info.loaded_at
        self._info = ImageStoreInfo(
            images=len(images),
            with_vectors=len(vectors),
            fingerprint=fingerprint if vectors else None,
            preprocess=preprocess if vectors else None,
            loaded_at=loaded_at if images else None,
        )
        report.seconds = time.perf_counter() - started
        return report

    async def info(self) -> ImageStoreInfo:
        return self._info

    async def search(self, vector: Sequence[float], *, limit: int) -> list[ImageMatch]:
        best: dict[str, tuple[float, str]] = {}  # subject → （负的相似度，图片 ID），越小越好
        for image_id, stored in self._vectors.items():
            candidate = (-dot(vector, stored), image_id)
            group = self._images[image_id].group
            if group not in best or candidate < best[group]:
                best[group] = candidate
        return [
            ImageMatch(self._images[image_id], -negative)
            for negative, image_id in sorted(best.values())[:limit]
        ]


_COLUMNS = (
    "i.image_id, i.kind, i.label, i.subject, i.page_title, i.url, i.image_url, i.text, "
    "i.meta, i.sha1"
)

_UPSERT = """
INSERT INTO kb_images (
    image_id, kind, label, subject, page_title, url, image_url, text, meta, sha1,
    embedding, fingerprint, preprocess, row_hash, updated_at
) VALUES (
    $1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10,
    $11::text::vector, $12, $13, $14, now()
)
ON CONFLICT (image_id) DO UPDATE SET
    kind = EXCLUDED.kind, label = EXCLUDED.label, subject = EXCLUDED.subject,
    page_title = EXCLUDED.page_title, url = EXCLUDED.url, image_url = EXCLUDED.image_url,
    text = EXCLUDED.text, meta = EXCLUDED.meta, sha1 = EXCLUDED.sha1,
    embedding = EXCLUDED.embedding, fingerprint = EXCLUDED.fingerprint,
    preprocess = EXCLUDED.preprocess, row_hash = EXCLUDED.row_hash, updated_at = now()
"""

# 查询向量先在 q 里算好一次（原因见 store.py 的 _VECTOR_SEARCH）。
# scored 给每张图打分，best 在每个 subject 里挑最像的一张，最后按分数排
_SEARCH = f"""
WITH q AS MATERIALIZED (SELECT $1::text::vector AS v),
scored AS (
    SELECT i.image_id,
           CASE WHEN i.subject = '' THEN i.image_id ELSE i.subject END AS subject_key,
           -(i.embedding <#> q.v) AS score
    FROM kb_images i, q
    WHERE i.embedding IS NOT NULL
),
best AS (
    SELECT DISTINCT ON (subject_key) image_id, score
    FROM scored
    ORDER BY subject_key, score DESC, image_id
)
SELECT {_COLUMNS}, b.score
FROM best b JOIN kb_images i USING (image_id)
ORDER BY b.score DESC, i.image_id
LIMIT $2
"""

_INFO = """
SELECT count(*) AS images, count(embedding) AS with_vectors,
       max(fingerprint) AS fingerprint, max(preprocess) AS preprocess,
       max(updated_at) AS loaded_at
FROM kb_images
"""


def _image(row: Any) -> KbImage:
    return KbImage(
        image_id=row["image_id"],
        kind=row["kind"],
        label=row["label"],
        page_title=row["page_title"],
        url=row["url"],
        image_url=row["image_url"],
        text=row["text"],
        subject=row["subject"],
        meta=json.loads(row["meta"]),
        sha1=row["sha1"],
    )


class PostgresImageStore:
    """落在 ``kb_images``（见 migrations/0004_kb_images.sql）。"""

    def __init__(self, db: Database):
        self._db = db

    async def sync(
        self, records: Iterable[ImageRecord], *, fingerprint: str, preprocess: str
    ) -> ImageSyncReport:
        started = time.perf_counter()
        report = ImageSyncReport()
        async with self._db.pool.acquire() as conn, conn.transaction():
            rows = await conn.fetch("SELECT image_id, row_hash FROM kb_images")
            existing = {row["image_id"]: row["row_hash"] for row in rows}
            seen: set[str] = set()
            batch: list[tuple[Any, ...]] = []
            for image, vector in records:
                report.total += 1
                seen.add(image.image_id)
                if vector is not None:
                    report.with_vectors += 1
                how = (fingerprint, preprocess) if vector is not None else (None, None)
                row_hash = image.row_hash(*how)
                if existing.get(image.image_id) == row_hash:
                    report.unchanged += 1
                    continue
                batch.append(
                    (
                        image.image_id,
                        image.kind,
                        image.label,
                        image.subject,
                        image.page_title,
                        image.url,
                        image.image_url,
                        image.text,
                        json.dumps(image.meta, ensure_ascii=False),
                        image.sha1,
                        vector_literal(vector) if vector is not None else None,
                        *how,
                        row_hash,
                    )
                )
                if len(batch) >= _UPSERT_BATCH:
                    await conn.executemany(_UPSERT, batch)
                    report.upserted += len(batch)
                    batch.clear()
            if batch:
                await conn.executemany(_UPSERT, batch)
                report.upserted += len(batch)
            gone = [image_id for image_id in existing if image_id not in seen]
            if gone:
                await conn.execute("DELETE FROM kb_images WHERE image_id = ANY($1::text[])", gone)
            report.deleted = len(gone)
        await self._db.pool.execute("ANALYZE kb_images")
        report.seconds = time.perf_counter() - started
        return report

    async def info(self) -> ImageStoreInfo:
        row = await self._db.pool.fetchrow(_INFO)
        loaded_at = row["loaded_at"]
        return ImageStoreInfo(
            images=int(row["images"]),
            with_vectors=int(row["with_vectors"]),
            fingerprint=row["fingerprint"],
            preprocess=row["preprocess"],
            loaded_at=loaded_at.astimezone().strftime("%Y-%m-%dT%H:%M:%S%z") if loaded_at else None,
        )

    async def search(self, vector: Sequence[float], *, limit: int) -> list[ImageMatch]:
        rows = await self._db.pool.fetch(_SEARCH, vector_literal(vector), limit)
        return [ImageMatch(_image(row), float(row["score"])) for row in rows]
