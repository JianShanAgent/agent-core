"""给切片算向量：读 ``chunks.jsonl`` → 调嵌入接口 → 存到本地向量文件，可断点续跑。

算的是切片的 ``text``（首行标题路径 + 正文），``embed: false`` 的切片不算。
实体标签是单独的字段，不在被算的文字里，改实体规则不用重算。

向量文件按提供方分目录（``<向量目录>/<提供方>/``），不同提供方的向量指纹不同，不会混在一起：

- ``meta.json``    指纹（提供方 + 型号 + 维度）、接口地址。目录里已有别的指纹的向量时拒绝写入
- ``vectors.f32``  向量本体：单精度、小端、已归一化，一条接一条，只往后追加
- ``index.jsonl``  第 N 行说明第 N 条向量是哪个切片的：``chunk_id`` 和被算文字的哈希 ``text_hash``

断点续跑：每批先写向量、再写索引，所以索引里有的一定已经落盘；重新打开时把没有索引的
半截向量截掉。重跑时 ``chunk_id`` 和 ``text_hash`` 都对得上的切片跳过，只算新的和文字变了的。
切片改了以后旧向量还留在文件里（不删），导入数据库时按当前切片取，报告里会数出有多少条是旧的。
打开时要修剪文件，所以一个目录同一时刻只让一个进程打开（目录里的 ``.lock`` 是加锁用的空文件）：
算向量没跑完时，别的进程来打开（看进度、导入）会被拒绝，等它跑完再来。

一批失败（重试后仍然不行）时记下来接着算别的，最后一起报告，重跑一次就只补这些；
连续失败多批（多半是密钥、余额或服务没起来）就停下，不白白重试到底。

图片索引用的是同一套向量文件和同一个续跑流程（:func:`embed_items`，调用方见 image_embed.py）：
那边 ``chunk_id`` 记的是图片 ID，``text_hash`` 记的是「原图哈希 + 预处理」。
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import random
import time
from array import array
from collections.abc import Awaitable, Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, Protocol

import httpx

from jianshanagent.core.embedding import (
    EmbeddingClient,
    EmbeddingError,
    EmbeddingProvider,
    EmbeddingResult,
    dot,
    vector_bytes,
    vector_from_bytes,
)
from jianshanagent.core.logging import get_logger

logger = get_logger(__name__)

META_FILE = "meta.json"
INDEX_FILE = "index.jsonl"
VECTORS_FILE = "vectors.f32"
#: 空文件，只用来加锁：同一个向量目录同一时刻只让一个进程打开
LOCK_FILE = ".lock"
#: 连续这么多批失败就停下
MAX_CONSECUTIVE_FAILURES = 3
#: 至少隔这么多秒报一次进度
PROGRESS_SECONDS = 30.0


class VectorStoreError(RuntimeError):
    pass


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class VectorStore:
    """一个提供方的向量文件。用法：``open()`` → ``append()`` … → ``close()``。"""

    def __init__(self, directory: Path, provider: EmbeddingProvider, *, embedded: str = "text"):
        self.directory = directory
        self.provider = provider
        #: 被算成向量的是什么，只在新建目录时写进 ``meta.json`` 备查
        self.embedded = embedded
        self.row_bytes = provider.dimensions * 4
        #: 第 N 条向量对应的（chunk_id, text_hash）
        self.rows: list[tuple[str, str]] = []
        self._row_of: dict[tuple[str, str], int] = {}
        self._vectors: IO[bytes] | None = None
        self._index: IO[str] | None = None
        self._lock: IO[bytes] | None = None

    def _read_index(self) -> list[tuple[str, str]]:
        path = self.directory / INDEX_FILE
        rows: list[tuple[str, str]] = []
        if not path.exists():
            return rows
        with path.open(encoding="utf-8") as lines:
            for line in lines:
                try:
                    item = json.loads(line)
                    rows.append((str(item["chunk_id"]), str(item["text_hash"])))
                except (ValueError, KeyError, TypeError):
                    break  # 中断时写了半行：它和它后面的都不算
        return rows

    def open(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        # 打开时会修剪索引和向量文件，所以同一时刻只让一个进程打开：算向量跑到一半时，
        # 另一个进程再来打开（哪怕只是看进度），前一个之后写的向量和索引就对不上了
        lock = (self.directory / LOCK_FILE).open("ab")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            lock.close()
            raise VectorStoreError(
                f"{self.directory} 正被另一个进程打开着（多半是算向量还没跑完），等它结束再来"
            ) from None
        self._lock = lock
        try:
            self._open()
        except BaseException:
            self.close()
            raise

    def _open(self) -> None:
        meta_path = self.directory / META_FILE
        fingerprint = self.provider.fingerprint
        if meta_path.exists():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if meta.get("fingerprint") != fingerprint:
                raise VectorStoreError(
                    f"{self.directory} 里已有指纹为「{meta.get('fingerprint')}」的向量，"
                    f"和现在的「{fingerprint}」不是同一种，不能混写。换一个目录，或先把旧的移走"
                )
        else:
            meta = {
                "fingerprint": fingerprint,
                "provider": self.provider.name,
                "model": self.provider.model,
                "dimensions": self.provider.dimensions,
                "base_url": self.provider.base_url,
                "format": "float32 小端，已归一化；第 N 条对应 index.jsonl 的第 N 行",
                "embedded_field": self.embedded,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            }
            meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

        rows = self._read_index()
        vectors_path = self.directory / VECTORS_FILE
        stored = vectors_path.stat().st_size // self.row_bytes if vectors_path.exists() else 0
        if stored < len(rows):  # 不该发生（先写向量后写索引）；发生了就以落盘的向量为准
            logger.warning("kb_vectors_index_ahead", index_rows=len(rows), vector_rows=stored)
            rows = rows[:stored]
        # 把索引和向量都修剪到一致：去掉半行索引、去掉没有索引的半截向量
        index_path = self.directory / INDEX_FILE
        tmp = index_path.with_suffix(".jsonl.tmp")
        with tmp.open("w", encoding="utf-8") as out:
            for chunk_id, digest in rows:
                out.write(json.dumps({"chunk_id": chunk_id, "text_hash": digest}) + "\n")
        os.replace(tmp, index_path)
        with vectors_path.open("ab") as fh:
            fh.truncate(len(rows) * self.row_bytes)

        self.rows = rows
        self._row_of = {key: i for i, key in enumerate(rows)}
        self._vectors = vectors_path.open("ab")
        self._index = index_path.open("a", encoding="utf-8")

    def close(self) -> None:
        for fh in (self._vectors, self._index, self._lock):  # 关掉锁文件，锁就放开了
            if fh is not None:
                fh.close()
        self._vectors = self._index = self._lock = None

    def has(self, chunk_id: str, digest: str) -> bool:
        return (chunk_id, digest) in self._row_of

    def row(self, chunk_id: str, digest: str) -> int | None:
        return self._row_of.get((chunk_id, digest))

    def append(self, items: list[tuple[str, str, array[float]]]) -> None:
        assert self._vectors is not None and self._index is not None, "先 open()"
        for _, _, vector in items:
            if len(vector) != self.provider.dimensions:
                raise VectorStoreError(
                    f"向量是 {len(vector)} 维，应为 {self.provider.dimensions} 维"
                )
        self._vectors.write(b"".join(vector_bytes(vector) for _, _, vector in items))
        self._vectors.flush()
        os.fsync(self._vectors.fileno())
        for chunk_id, digest, _ in items:
            self._index.write(json.dumps({"chunk_id": chunk_id, "text_hash": digest}) + "\n")
            self._row_of[(chunk_id, digest)] = len(self.rows)
            self.rows.append((chunk_id, digest))
        self._index.flush()

    def vector(self, row: int) -> array[float]:
        with (self.directory / VECTORS_FILE).open("rb") as fh:
            fh.seek(row * self.row_bytes)
            return vector_from_bytes(fh.read(self.row_bytes))


class Embeddable(Protocol):
    """要算向量的一项：``chunk_id`` 是它的标识，``digest`` 是被算内容的哈希（内容变了就重算）。"""

    @property
    def chunk_id(self) -> str: ...

    @property
    def digest(self) -> str: ...


@dataclass(frozen=True, slots=True)
class Pending:
    chunk_id: str
    text: str
    digest: str


def read_embeddable(chunks_path: Path) -> Iterator[Pending]:
    with chunks_path.open(encoding="utf-8") as lines:
        for line in lines:
            chunk = json.loads(line)
            if chunk.get("embed", True) and chunk.get("text"):
                yield Pending(chunk["chunk_id"], chunk["text"], text_hash(chunk["text"]))


@dataclass(slots=True)
class EmbedReport:
    fingerprint: str
    #: 该算向量的切片总数
    total: int = 0
    #: 开始前就已经有向量的
    already: int = 0
    #: 这次算出来的
    embedded: int = 0
    #: 这次没算成的切片
    failed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    #: 连续失败而提前停下
    stopped: bool = False
    #: 向量文件里不属于当前切片的旧向量条数
    stale: int = 0
    prompt_tokens: int = 0
    seconds: float = 0.0

    @property
    def missing(self) -> int:
        return self.total - self.already - self.embedded

    def to_json(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "total": self.total,
            "already": self.already,
            "embedded": self.embedded,
            "missing": self.missing,
            "failed": len(self.failed),
            "errors": self.errors[:5],
            "stopped": self.stopped,
            "stale": self.stale,
            "prompt_tokens": self.prompt_tokens,
            "seconds": round(self.seconds, 1),
        }


def status_of(
    provider: EmbeddingProvider,
    items: Iterable[Embeddable],
    store_dir: Path,
    *,
    embedded: str = "text",
) -> EmbedReport:
    """只看现状，不调接口：``items`` 里有多少已经有向量，向量文件里又有多少是旧的。"""
    store = VectorStore(store_dir, provider, embedded=embedded)
    store.open()
    store.close()
    current = {(p.chunk_id, p.digest) for p in items}
    have = current & set(store.rows)
    return EmbedReport(
        fingerprint=provider.fingerprint,
        total=len(current),
        already=len(have),
        stale=len(set(store.rows) - current),
    )


def store_status(provider: EmbeddingProvider, chunks_path: Path, store_dir: Path) -> EmbedReport:
    """只看现状，不调接口。"""
    return status_of(provider, read_embeddable(chunks_path), store_dir)


async def _embed_texts(client: EmbeddingClient, batch: list[Pending]) -> EmbeddingResult:
    return await client.embed([p.text for p in batch])


async def embed_chunks(
    provider: EmbeddingProvider,
    chunks_path: Path,
    store_dir: Path,
    *,
    limit: int | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    retry_base_seconds: float = 1.0,
) -> EmbedReport:
    """给还没有向量的切片算向量。``limit``：这次最多算多少片（试跑用）。"""
    # 长的先算：长度相近的排在一批里（本机模型少做填充），而且超长之类的问题一开始就暴露
    everything = sorted(read_embeddable(chunks_path), key=lambda p: -len(p.text))
    return await embed_items(
        provider,
        everything,
        store_dir,
        _embed_texts,
        limit=limit,
        transport=transport,
        retry_base_seconds=retry_base_seconds,
    )


async def embed_items[T: Embeddable](
    provider: EmbeddingProvider,
    everything: Sequence[T],
    store_dir: Path,
    embed_batch: Callable[[EmbeddingClient, list[T]], Awaitable[EmbeddingResult]],
    *,
    batch_size: int | None = None,
    limit: int | None = None,
    embedded: str = "text",
    transport: httpx.AsyncBaseTransport | None = None,
    retry_base_seconds: float = 1.0,
) -> EmbedReport:
    """给 ``everything`` 里还没有向量的算向量，按给的顺序分批；可断点续跑。

    ``embed_batch`` 把一批算成向量（文字直接调接口；图片要先读原图、做预处理）。
    ``batch_size`` 不给就用提供方配置的；``limit``：这次最多算多少条（试跑用）。
    """
    store = VectorStore(store_dir, provider, embedded=embedded)
    store.open()
    report = EmbedReport(fingerprint=provider.fingerprint)
    client = EmbeddingClient(provider, transport=transport, retry_base_seconds=retry_base_seconds)
    try:
        current = {(p.chunk_id, p.digest) for p in everything}
        report.total = len(current)
        report.stale = len(set(store.rows) - current)
        pending = [p for p in everything if not store.has(p.chunk_id, p.digest)]
        report.already = report.total - len(pending)
        if limit is not None:
            pending = pending[:limit]
        size = batch_size or provider.batch_size
        batches = [pending[i : i + size] for i in range(0, len(pending), size)]
        logger.info(
            "kb_embed_start",
            fingerprint=provider.fingerprint,
            total=report.total,
            already=report.already,
            to_embed=len(pending),
            batches=len(batches),
            stale=report.stale,
        )

        started = time.perf_counter()
        last_progress = started
        consecutive_failures = 0
        gate = asyncio.Semaphore(provider.concurrency)

        async def work(batch: list[T]) -> None:
            nonlocal consecutive_failures, last_progress
            async with gate:
                if report.stopped:
                    return
                try:
                    result = await embed_batch(client, batch)
                except EmbeddingError as exc:
                    consecutive_failures += 1
                    report.failed.extend(p.chunk_id for p in batch)
                    report.errors.append(str(exc)[:300])
                    logger.warning("kb_embed_batch_failed", chunks=len(batch), error=str(exc)[:300])
                    if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                        report.stopped = True
                    return
                consecutive_failures = 0
                store.append(
                    [
                        (p.chunk_id, p.digest, vector)
                        for p, vector in zip(batch, result.vectors, strict=True)
                    ]
                )
                report.embedded += len(batch)
                report.prompt_tokens += result.prompt_tokens
                now = time.perf_counter()
                if now - last_progress >= PROGRESS_SECONDS or report.embedded == len(pending):
                    last_progress = now
                    rate = report.embedded / max(now - started, 1e-6)
                    left = len(pending) - report.embedded - len(report.failed)
                    logger.info(
                        "kb_embed_progress",
                        done=report.embedded,
                        total=len(pending),
                        failed=len(report.failed),
                        tokens=report.prompt_tokens,
                        per_second=round(rate, 2),
                        eta_minutes=round(left / rate / 60, 1) if rate else None,
                    )

        await asyncio.gather(*(work(batch) for batch in batches))
        report.seconds = time.perf_counter() - started
        logger.info("kb_embed_done", **report.to_json())
        return report
    finally:
        await client.aclose()
        store.close()


@dataclass(frozen=True, slots=True)
class VerifyResult:
    checked: int
    mean: float
    lowest: float
    lowest_chunk_id: str | None


async def verify_store(
    provider: EmbeddingProvider,
    chunks_path: Path,
    store_dir: Path,
    *,
    sample: int = 20,
    seed: int = 20261111,
    transport: httpx.AsyncBaseTransport | None = None,
) -> VerifyResult:
    """随机抽几片，每片单独再算一次，和存下来的向量比（相似度应接近 1）。

    单独算和当初成批算的分批、顺序都不一样，所以能查出「向量和切片对错了位」这类问题。
    """
    return await verify_items(
        provider,
        list(read_embeddable(chunks_path)),
        store_dir,
        _embed_texts,
        sample=sample,
        seed=seed,
        transport=transport,
    )


async def verify_items[T: Embeddable](
    provider: EmbeddingProvider,
    everything: Sequence[T],
    store_dir: Path,
    embed_batch: Callable[[EmbeddingClient, list[T]], Awaitable[EmbeddingResult]],
    *,
    sample: int = 20,
    seed: int = 20261111,
    embedded: str = "text",
    transport: httpx.AsyncBaseTransport | None = None,
) -> VerifyResult:
    """从已有向量的里面随机抽几条，每条单独再算一次，和存下来的比。"""
    store = VectorStore(store_dir, provider, embedded=embedded)
    store.open()
    store.close()
    stored = [p for p in everything if store.has(p.chunk_id, p.digest)]
    picked = random.Random(seed).sample(stored, min(sample, len(stored)))
    client = EmbeddingClient(provider, transport=transport)
    scores: list[tuple[float, str]] = []
    try:
        for item in picked:
            row = store.row(item.chunk_id, item.digest)
            assert row is not None
            again = (await embed_batch(client, [item])).vectors[0]
            scores.append((dot(again, store.vector(row)), item.chunk_id))
    finally:
        await client.aclose()
    if not scores:
        return VerifyResult(0, 0.0, 0.0, None)
    lowest = min(scores)
    return VerifyResult(
        checked=len(scores),
        mean=sum(score for score, _ in scores) / len(scores),
        lowest=lowest[0],
        lowest_chunk_id=lowest[1],
    )
