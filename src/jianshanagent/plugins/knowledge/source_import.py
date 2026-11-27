"""离线文档导入：有界容量、增量候选、事务同步与可恢复发布。

持来源 flock 后先生成不可变候选及 journal，再事务 store.sync，最后原子切换 current。
发布前失败恢复旧 current；崩溃恢复重放 current。完成/恢复后只保留 current 与 previous，
journal 尚在时不回收任何世代。GC 不用于腾出容量后继续导入：容量检查失败直接拒绝。
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import math
import os
import re
import shutil
import stat
import uuid
from array import array
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from jianshanagent.plugins.knowledge.source_import_v2 import StreamImportLimits

from jianshanagent.core.config import AppConfig
from jianshanagent.core.embedding import EmbeddingProvider, EmbeddingSettings
from jianshanagent.core.sources import SourceInstance, load_source_adapter, source_instance
from jianshanagent.plugins.knowledge.chunker import ChunkStats, chunk_document
from jianshanagent.plugins.knowledge.embed import VectorStore, embed_chunks, text_hash, verify_store
from jianshanagent.plugins.knowledge.load import read_records
from jianshanagent.plugins.knowledge.models import Document
from jianshanagent.plugins.knowledge.settings import ChunkSettings, kb_root
from jianshanagent.plugins.knowledge.store import KbChunk, KnowledgeStore, Record

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_RECORDS = 10_000
MAX_LINE_BYTES = 512 * 1024
VERIFY_MIN_SIMILARITY = 0.99
_CONTROL_BYTES = 64 * 1024
_GENERATION = re.compile(r"[0-9a-f]{32}\Z")
_RUNNING = {"validating", "embedding", "committing"}
Embedder = Callable[[EmbeddingProvider, Path, Path], Awaitable[float]]


class SourceImportError(ValueError):
    """不含提供方凭据与原始接口报错的导入错误。"""


class SourceImportPublishedError(SourceImportError):
    """资料已发布，不能按导入失败或回滚处理。"""

    published = True
    public_message = "资料已发布，但状态记录待确认；请查询状态后重试"

    def __init__(self) -> None:
        super().__init__(self.public_message)


CAPACITY_PUBLIC_MESSAGE = "来源资料超过安全容量或剩余磁盘预算，旧资料保持不变。"
CAPACITY_ERROR_CODES = frozenset(
    {
        "documents",
        "chunks",
        "metadata_bytes",
        "vector_bytes",
        "snapshot_bytes",
        "working_bytes",
        "snapshot_read_bytes",
        "snapshot_read_memory",
        "metadata_read_memory",
        "disk_free_bytes",
        "resource_measurement",
    }
)


class SourceImportCapacityError(SourceImportError):
    """可公开展示的固定容量错误；code 是受控诊断码，不含路径或提供方内容。"""

    public_message = CAPACITY_PUBLIC_MESSAGE

    def __init__(self, code: str):
        if code not in CAPACITY_ERROR_CODES:
            raise ValueError("未登记的容量错误代码")
        self.code = code
        super().__init__(self.public_message)


@dataclass(frozen=True, slots=True)
class ImportLimits:
    """服务级保守预算，可由测试/受控宿主注入，不读取任意来源 YAML 参数。"""

    max_documents: int = 2000
    max_chunks: int = 4000
    max_vector_bytes: int = 32 * 1024 * 1024
    max_snapshot_bytes: int = 32 * 1024 * 1024
    max_metadata_bytes: int = 8 * 1024 * 1024
    max_working_bytes: int = 512 * 1024 * 1024
    disk_safety_bytes: int = 128 * 1024 * 1024

    def __post_init__(self) -> None:
        for item in fields(self):
            value = getattr(self, item.name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"ImportLimits.{item.name} 必须为正整数")


class _Cancelled(Exception):
    pass


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _read_json(path: Path, max_bytes: int = _CONTROL_BYTES) -> Any:
    """先 stat 再有界读取；不把旧大快照无界 loads，也不跟随符号链接。"""
    if path.is_symlink():
        raise SourceImportError("来源文件不能是符号链接")
    if path.stat().st_size > max_bytes:
        raise SourceImportCapacityError("snapshot_read_bytes")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise SourceImportError("来源文件必须是普通文件")
        raw = source.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise SourceImportCapacityError("snapshot_read_bytes")
    return json.loads(raw)


def _atomic_json(path: Path, value: Any) -> None:
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temp.open("x", encoding="utf-8") as out:
            json.dump(value, out, ensure_ascii=False, allow_nan=False)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, path)
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        temp.unlink(missing_ok=True)


def _json_bytes(value: Any) -> int:
    # 不先 dumps 整串；单个合法正文块已有单条上限。
    encoder = json.JSONEncoder(ensure_ascii=False, allow_nan=False)
    return sum(len(part.encode("utf-8")) for part in encoder.iterencode(value))


async def _blocking(function: Callable[..., Any], *args: Any) -> Any:
    """取消时也等工作线程退出，避免提前释放 flock 后线程仍在改同一来源文件。"""
    work = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(work)
    except asyncio.CancelledError:
        while not work.done():
            try:
                await asyncio.shield(work)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if work.done() and not work.cancelled():
            try:
                work.result()
            except Exception:
                pass
        raise


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    row: dict[str, Any] = {}
    for key, value in pairs:
        if key in row:
            raise ValueError(f"重复 JSON 字段：{key}")
        row[key] = value
    return row


def parse_jsonl(content: str | bytes) -> list[dict[str, Any]]:
    if not isinstance(content, (str, bytes)):
        raise SourceImportError("只接受 UTF-8 JSONL 内容")
    raw = content.encode("utf-8") if isinstance(content, str) else content
    if len(raw) > MAX_FILE_BYTES:
        raise SourceImportError(f"文件超过 {MAX_FILE_BYTES} 字节限制")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise SourceImportError("文件必须是 UTF-8 JSONL") from None
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        if len(line.encode("utf-8")) > MAX_LINE_BYTES:
            raise SourceImportError(f"第 {number} 行超过长度限制")
        if len(rows) >= MAX_RECORDS:
            raise SourceImportError(f"记录数超过 {MAX_RECORDS}")
        try:
            row = json.loads(line, object_pairs_hook=_reject_duplicate_keys)
        except (ValueError, RecursionError):
            raise SourceImportError(f"第 {number} 行不是合法且无重复字段的 JSON 对象") from None
        if not isinstance(row, dict):
            raise SourceImportError(f"第 {number} 行必须是 JSON 对象")
        rows.append(row)
    return rows


class SourceImportService:
    def __init__(
        self,
        config: AppConfig,
        store: KnowledgeStore,
        embedder: Embedder | None = None,
        *,
        limits: ImportLimits | None = None,
        config_provider: Callable[[], AppConfig] | None = None,
        v2_limits: StreamImportLimits | None = None,
        resource_probe: Callable[[list[str]], Awaitable[dict[str, Any]]] | None = None,
    ):
        from jianshanagent.plugins.knowledge.source_import_v2 import StreamImportLimits

        self.v2_limits = v2_limits if v2_limits is not None else StreamImportLimits()
        self.resource_probe = resource_probe
        self.config = config
        self.store = store
        self.embedder = embedder
        self.limits = limits if limits is not None else ImportLimits()
        self._config_provider = config_provider
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._cancelled: set[str] = set()
        self._states: dict[str, dict[str, Any]] = {}
        self._closed = False

    def _directory(self, source_id: str, *, config: AppConfig | None = None) -> Path:
        if config is None:
            config = self.config
        source = source_instance(config, source_id)
        if source.driver != "documents":
            raise SourceImportError("该来源不是标准文档导入来源")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", source_id):
            raise SourceImportError("来源 ID 不能作为安全命名空间")
        root = kb_root(config)
        directory = root / source_id / "source-import"
        for item in (root / source_id, directory, directory / "generations"):
            if item.is_symlink():
                raise SourceImportError("来源目录不能是符号链接")
        return directory

    @staticmethod
    def _embedding_provider(config: AppConfig) -> EmbeddingProvider | None:
        embedding = EmbeddingSettings.from_config(config)
        return embedding.provider() if embedding is not None else None

    def _check_commit_config(
        self, source: SourceInstance, directory: Path, provider: EmbeddingProvider | None
    ) -> None:
        try:
            config = self._config_provider() if self._config_provider is not None else self.config
            current = self._embedding_provider(config)
            if (
                source_instance(config, source.id) == source
                and self._directory(source.id, config=config) == directory
                and (current.fingerprint if current is not None else None)
                == (provider.fingerprint if provider is not None else None)
            ):
                return
        except Exception:
            # A failed reload is unsafe too; don't expose raw configuration errors.
            pass
        raise SourceImportError("导入期间来源或嵌入配置已变化或不可用，请按新配置重试")

    def _check_authors(
        self, source_id: str, rows: list[dict[str, Any]], documents: list[Document]
    ) -> None:
        collector = source_instance(self.config, source_id).raw.get("collector")
        if collector is not None:
            uid = collector["uid"]
            for index, (row, doc) in enumerate(zip(rows, documents, strict=True), 1):
                # The unmodified row is persisted and must pass the same ownership check later.
                if row.get("author_id") != uid or doc.meta.get("author_id") != uid:
                    raise SourceImportError(f"第 {index} 条：作者 ID 必须与来源绑定的微博 UID 一致")

    @staticmethod
    def _generation(directory: Path, value: str) -> Path:
        if not isinstance(value, str) or not _GENERATION.fullmatch(value):
            raise SourceImportError("来源世代记录损坏，需要管理员恢复")
        path = directory / "generations" / value
        if path.parent.is_symlink() or path.is_symlink():
            raise SourceImportError("来源世代不能是符号链接")
        return path

    def _current(self, directory: Path) -> str | None:
        path = directory / "current.json"
        if not path.exists():
            return None
        generation: str = _read_json(path)["generation"]
        self._generation(directory, generation)
        return generation

    def _guard_snapshot(self, directory: Path, generation: str) -> Path:
        path = self._generation(directory, generation) / "snapshot.json"
        if path.is_symlink():
            raise SourceImportError("来源快照不能是符号链接")
        size = path.stat().st_size
        if size > self.limits.max_snapshot_bytes:
            raise SourceImportCapacityError("snapshot_read_bytes")
        # 已知 JSON 数据结构的保守解码放大预算，拒绝发生在 json.loads 之前。
        if size * 16 > self.limits.max_working_bytes:
            raise SourceImportCapacityError("snapshot_read_memory")
        return path

    def _snapshot(self, directory: Path, generation: str | None) -> dict[str, Any] | None:
        if generation is None:
            return None
        value: dict[str, Any] = _read_json(
            self._guard_snapshot(directory, generation), self.limits.max_snapshot_bytes
        )
        if len(value["rows"]) > self.limits.max_documents:
            raise SourceImportCapacityError("documents")
        if len(value["records"]) > self.limits.max_chunks:
            raise SourceImportCapacityError("chunks")
        vector_bytes = sum(len(row["vector"]) * 4 for row in value["records"])
        if vector_bytes > self.limits.max_vector_bytes:
            raise SourceImportCapacityError("vector_bytes")
        return value

    def _metadata(self, directory: Path, generation: str | None) -> dict[str, Any] | None:
        if generation is None:
            return None
        self._guard_snapshot(directory, generation)
        path = self._generation(directory, generation) / "documents.json"
        value: dict[str, Any]
        if path.exists():
            if path.stat().st_size * 16 > self.limits.max_working_bytes:
                raise SourceImportCapacityError("metadata_read_memory")
            value = _read_json(path, self.limits.max_metadata_bytes)
        else:
            # 兼容首版世代；旧快照仍先受读取/解码预算限制。
            old = self._snapshot(directory, generation)
            assert old is not None
            value = {
                "rows": old["rows"],
                "chunks": [r["chunk"] for r in old["records"]],
                "fingerprint": old["fingerprint"],
            }
        if len(value["rows"]) > self.limits.max_documents:
            raise SourceImportCapacityError("documents")
        if len(value["chunks"]) > self.limits.max_chunks:
            raise SourceImportCapacityError("chunks")
        return value

    def _capacity(
        self, rows: list[dict[str, Any]], chunks: list[dict[str, Any]], dimensions: int
    ) -> dict[str, int]:
        limits = self.limits
        if type(dimensions) is not int or dimensions <= 0:
            raise SourceImportCapacityError("vector_bytes")
        if len(rows) > limits.max_documents:
            raise SourceImportCapacityError("documents")
        if len(chunks) > limits.max_chunks:
            raise SourceImportCapacityError("chunks")
        metadata_bytes = _json_bytes({"rows": rows, "chunks": chunks}) + 1024
        values = len(chunks) * dimensions
        vector_bytes = values * 4
        # float32 的 JSON 表示以每值32字节预留，不因测试向量恰为0/1而低估真实4096维。
        snapshot_bytes = metadata_bytes + values * 32 + len(chunks) * 128
        working_bytes = metadata_bytes * 6 + values * 48 + snapshot_bytes * 2
        for code, actual, maximum in (
            ("metadata_bytes", metadata_bytes, limits.max_metadata_bytes),
            ("vector_bytes", vector_bytes, limits.max_vector_bytes),
            ("snapshot_bytes", snapshot_bytes, limits.max_snapshot_bytes),
            ("working_bytes", working_bytes, limits.max_working_bytes),
        ):
            if actual > maximum:
                raise SourceImportCapacityError(code)
        return {
            "documents": len(rows),
            "chunks": len(chunks),
            "dimensions": dimensions,
            "vector_bytes": vector_bytes,
            "metadata_bytes": metadata_bytes,
            "snapshot_bytes": snapshot_bytes,
            "working_bytes": working_bytes,
            "generation_bytes": snapshot_bytes
            + vector_bytes
            + metadata_bytes * 3
            + len(chunks) * 512,
        }

    def _candidate(
        self, source_id: str, content: str | bytes
    ) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
        directory = self._directory(source_id)
        if (directory / "journal.json").exists():
            raise SourceImportError("上次导入待恢复；请重新确认导入以恢复后重试")
        rows = parse_jsonl(content)
        if len(rows) > self.limits.max_documents:
            raise SourceImportCapacityError("documents")
        adapter = load_source_adapter(self.config, source_id)
        try:
            incoming = adapter.normalize(rows)
        except ValueError as exc:
            raise SourceImportError(str(exc)) from None
        self._check_authors(source_id, rows, incoming)
        previous = self._metadata(directory, self._current(directory))
        old_rows = previous["rows"] if previous else []
        old_docs = adapter.normalize(old_rows)
        old = {doc.doc_key: asdict(doc) for doc in old_docs}
        merged = {doc.doc_key: row for doc, row in zip(old_docs, old_rows, strict=True)}
        new = updated = unchanged = 0
        for doc, row in zip(incoming, rows, strict=True):
            if doc.doc_key not in old:
                new += 1
            elif asdict(doc) == old[doc.doc_key]:
                unchanged += 1
            else:
                updated += 1
            merged[doc.doc_key] = row
            if len(merged) > self.limits.max_documents:
                raise SourceImportCapacityError("documents")
        all_rows = [merged[key] for key in sorted(merged)]
        documents = adapter.normalize(all_rows)
        chunks = []
        for doc in documents:
            for chunk in chunk_document(doc, ChunkSettings(), ChunkStats()):
                chunks.append(chunk.to_json())
                if len(chunks) > self.limits.max_chunks:
                    raise SourceImportCapacityError("chunks")
        embedding = EmbeddingSettings.from_config(self.config)
        dimensions = embedding.provider().dimensions if embedding is not None else 4096
        capacity = self._capacity(all_rows, chunks, dimensions)
        if previous and rows and chunks != previous["chunks"]:
            if not new and not updated:
                updated, unchanged = unchanged, 0
        report = {
            "source_id": source_id,
            "records": len(rows),
            "new": new,
            "updated": updated,
            "unchanged": unchanged,
            "total_documents": len(documents),
            "chunks": len(chunks),
            "empty": not rows,
            "capacity": capacity,
            "sample": [
                {
                    "id": doc.meta.get("post_id"),
                    "title": doc.title,
                    "text": "\n\n".join(b.render() for b in doc.blocks)[:1000],
                    "url": doc.url,
                    "published_at": doc.meta.get("published_at"),
                    "author_id": doc.meta.get("author_id"),
                    "author_name": doc.meta.get("author_name"),
                }
                for doc in incoming[:5]
            ],
        }
        return report, all_rows, chunks

    def preview(self, source_id: str, content: str | bytes) -> dict[str, Any]:
        """同步、零写入/零嵌入；后台应使用 asyncio.to_thread 调用。"""
        from jianshanagent.plugins.knowledge.source_import_v2 import (
            StreamSourceImporter,
            uses_stream_v2,
        )

        if uses_stream_v2(self.config, source_id):
            return StreamSourceImporter(self).candidate(source_id, content)[0]
        return self._candidate(source_id, content)[0]

    @staticmethod
    def _import_lock_held(directory: Path) -> bool:
        try:
            fd = os.open(
                directory / "import.lock",
                os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0),
            )
        except FileNotFoundError:
            return False
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise SourceImportError("导入锁必须是普通文件")
            try:
                # Only an exclusive holder signals an import; other readers do not.
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            return False
        finally:
            os.close(fd)

    def active(self, source_id: str) -> bool:
        return source_id in self._tasks or self._import_lock_held(self._directory(source_id))

    def status(self, source_id: str) -> dict[str, Any] | None:
        directory = self._directory(source_id)
        # Probe before reading state so a finishing worker isn't mistaken for an interrupted one.
        active = source_id in self._tasks or self._import_lock_held(directory)
        try:
            state = _read_json(directory / "status.json")
        except FileNotFoundError:
            state = None
        if active:
            return dict(state) if state else None
        journal = (directory / "journal.json").exists()
        running = state and state.get("state") in _RUNNING
        if (journal or running) and not self._import_lock_held(directory):
            if journal:
                return {
                    **(state or {}),
                    "source_id": source_id,
                    "state": "recovery_required",
                    "retryable": True,
                }
            if state.get("generation") and state["generation"] == self._current(directory):
                return {**state, "state": "completed", "published": True}
            return {**state, "state": "interrupted", "retryable": True}
        return dict(state) if state else None

    def cancel(self, source_id: str) -> bool:
        if source_id not in self._tasks or self._states[source_id].get("state") == "committing":
            return False
        self._cancelled.add(source_id)
        return True

    def _state(self, directory: Path, namespace: str, state: str, **extra: Any) -> dict[str, Any]:
        value = {
            **self._states.get(namespace, {}),
            **extra,
            "source_id": namespace,
            "state": state,
            "updated_at": _now(),
        }
        _atomic_json(directory / "status.json", value)
        self._states[namespace] = value
        return value

    def _check_cancelled(self, source_id: str) -> None:
        if source_id in self._cancelled:
            raise _Cancelled()

    @staticmethod
    def _records(snapshot: dict[str, Any], source_id: str) -> list[Record]:
        return [
            (KbChunk.from_json(row["chunk"], source_id), row["vector"])
            for row in snapshot["records"]
        ]

    async def _restore(self, directory: Path, source_id: str) -> None:
        journal = _read_json(directory / "journal.json")
        current = await _blocking(self._snapshot, directory, self._current(directory))
        records = await _blocking(self._records, current, source_id) if current else []
        await self.store.sync(
            records,
            fingerprint=current["fingerprint"] if current else journal["fingerprint"],
            wiki=source_id,
        )
        (directory / "journal.json").unlink()

    @staticmethod
    def _tree_bytes(path: Path) -> int:
        if path.is_symlink():
            raise SourceImportError("世代文件不能是符号链接")
        if not path.exists():
            return 0
        if path.is_file():
            return path.stat().st_size
        return sum(SourceImportService._tree_bytes(entry) for entry in path.iterdir())

    def _check_disk(
        self,
        directory: Path,
        capacity: dict[str, int],
        previous_id: str | None,
        remaining: bool = False,
    ) -> None:
        rollback = self._tree_bytes(self._generation(directory, previous_id)) if previous_id else 0
        required = self.limits.disk_safety_bytes + rollback
        if not remaining:
            required += capacity["generation_bytes"]
        if shutil.disk_usage(directory).free < required:
            raise SourceImportCapacityError("disk_free_bytes")
        capacity["disk_required_bytes"] = required
        capacity["rollback_bytes"] = rollback

    def _gc(self, directory: Path) -> dict[str, Any]:
        """GC 故障只报告待清理，不能把完整发布误报为失败，更不能猜测引用后删除。"""
        try:
            return self._gc_unchecked(directory)
        except (OSError, ValueError, KeyError, TypeError):
            return {"cleanup_pending": True}

    def _gc_unchecked(self, directory: Path) -> dict[str, Any]:
        """调用者必须持来源 flock；未恢复 journal 时不回收，永不跟随世代 symlink。"""
        if (directory / "journal.json").exists() or (directory / "journal.json").is_symlink():
            return {"cleanup_pending": True}
        keep = set()
        pointer = directory / "current.json"
        if pointer.is_symlink():
            return {"cleanup_pending": True}
        if pointer.exists():
            value = _read_json(pointer)
            for name in ("generation", "previous"):
                if value.get(name) is not None:
                    self._generation(directory, value[name])
                    keep.add(value[name])
        root = directory / "generations"
        if not root.exists():
            return {"cleanup_pending": False}
        if root.is_symlink():
            return {"cleanup_pending": True}
        pending = False
        for entry in root.iterdir():
            if not _GENERATION.fullmatch(entry.name) or entry.name in keep:
                continue
            if entry.is_symlink() or not entry.is_dir():
                pending = True
                continue
            try:
                shutil.rmtree(entry)  # rmtree 不跟随目录内的符号链接。
            except OSError:
                pending = True
        return {"cleanup_pending": pending}

    def _prepare_stage(
        self,
        stage: Path,
        chunks: list[dict[str, Any]],
        previous: dict[str, Any] | None,
        provider: EmbeddingProvider,
    ) -> None:
        stage.mkdir(parents=True)
        old = {
            (r["chunk"]["chunk_id"], r["chunk"]["text"]): r["vector"]
            for r in (previous["records"] if previous else [])
        }
        vectors = VectorStore(stage / "vectors", provider)
        vectors.open()
        try:
            with (
                (stage / "chunks.jsonl").open("w", encoding="utf-8") as out,
                (stage / "verify.jsonl").open("w", encoding="utf-8") as verify,
            ):
                for row in chunks:
                    line = json.dumps(row, ensure_ascii=False) + "\n"
                    out.write(line)
                    vector = old.get((row["chunk_id"], row["text"]))
                    if vector is None:
                        verify.write(line)
                    else:
                        # 只复用当前候选仍需要的向量；不复制永久增长的历史 index/cache。
                        vectors.append(
                            [(row["chunk_id"], text_hash(row["text"]), array("f", vector))]
                        )
                out.flush()
                os.fsync(out.fileno())
        finally:
            vectors.close()

    def _write_snapshot(
        self,
        stage: Path,
        rows: list[dict[str, Any]],
        chunks: list[dict[str, Any]],
        provider: EmbeddingProvider,
        source_id: str,
    ) -> list[Record]:
        records = list(read_records(stage / "chunks.jsonl", stage / "vectors", provider, source_id))
        if not records or any(vector is None for _, vector in records):
            raise SourceImportError("候选向量不完整，旧资料保持不变")
        serial_records = []
        for row, (_, vector) in zip(chunks, records, strict=True):
            assert vector is not None
            if len(vector) != provider.dimensions or not all(math.isfinite(v) for v in vector):
                raise SourceImportError("候选向量维度或数值无效")
            serial_records.append({"chunk": row, "vector": list(vector)})
        snapshot = {
            "version": 1,
            "rows": rows,
            "records": serial_records,
            "fingerprint": provider.fingerprint,
        }
        size = _json_bytes(snapshot)
        if size > self.limits.max_snapshot_bytes:
            raise SourceImportCapacityError("snapshot_bytes")
        _atomic_json(stage / "snapshot.json", snapshot)
        metadata = {
            "version": 1,
            "rows": rows,
            "chunks": chunks,
            "fingerprint": provider.fingerprint,
        }
        if _json_bytes(metadata) > self.limits.max_metadata_bytes:
            raise SourceImportCapacityError("metadata_bytes")
        _atomic_json(stage / "documents.json", metadata)
        fd = os.open(stage.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        return records

    async def _embed(
        self, provider: EmbeddingProvider, chunks: Path, vectors: Path, verify_chunks: Path
    ) -> None:
        if self.embedder is not None:
            minimum = await self.embedder(provider, chunks, vectors)
        else:
            await embed_chunks(provider, chunks, vectors)
            if verify_chunks.stat().st_size:
                minimum = (await verify_store(provider, verify_chunks, vectors, sample=20)).lowest
            else:
                minimum = 1.0
        if not isinstance(minimum, (int, float)) or not math.isfinite(minimum):
            raise SourceImportError("嵌入验证没有返回合法相似度")
        if minimum < VERIFY_MIN_SIMILARITY:
            raise SourceImportError("嵌入验证相似度低于 0.99，旧资料保持不变")

    def _validate_input(self, source_id: str, content: str | bytes) -> None:
        incoming = parse_jsonl(content)
        if len(incoming) > self.limits.max_documents:
            raise SourceImportCapacityError("documents")
        documents = load_source_adapter(self.config, source_id).normalize(incoming)
        self._check_authors(source_id, incoming, documents)

    async def import_data(self, source_id: str, content: str | bytes) -> dict[str, Any]:
        from jianshanagent.plugins.knowledge.source_import_v2 import (
            StreamSourceImporter,
            uses_stream_v2,
        )

        if uses_stream_v2(self.config, source_id):
            return await StreamSourceImporter(self).import_data(source_id, content)
        if self._closed:
            raise SourceImportError("导入服务已关闭")
        if source_id in self._tasks:
            raise SourceImportError("该来源已有导入任务")
        directory = self._directory(source_id)
        source = source_instance(self.config, source_id)
        if not source.mounted:
            raise SourceImportError("来源已停用，请恢复后导入")
        # Capture the initial fingerprint before any await, but don't let an invalid
        # embedding configuration skip recovery of a previous transaction.
        startup_embedding: EmbeddingProvider | Exception | None
        try:
            startup_embedding = self._embedding_provider(self.config)
        except Exception as exc:
            startup_embedding = exc
        await _blocking(self._validate_input, source_id, content)
        if source_id in self._tasks:
            raise SourceImportError("该来源已有导入任务")
        directory.mkdir(parents=True, exist_ok=True)
        lock = (directory / "import.lock").open("ab")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            lock.close()
            raise SourceImportError("该来源正由另一个导入任务处理") from None
        task = asyncio.current_task()
        assert task is not None
        self._tasks[source_id] = task
        self._cancelled.discard(source_id)
        self._states[source_id] = {"job_id": uuid.uuid4().hex, "started_at": _now()}
        committing = published = False
        try:
            if (directory / "journal.json").exists():
                await self._restore(directory, source_id)
                await _blocking(self._gc, directory)
            self._state(directory, source_id, "validating")
            if isinstance(startup_embedding, Exception):
                raise startup_embedding
            provider = startup_embedding
            report, rows, chunks = await _blocking(self._candidate, source_id, content)
            self._check_cancelled(source_id)
            if report["empty"] or (not report["new"] and not report["updated"]):
                await self.store.info()
                cleanup = await _blocking(self._gc, directory)
                await _blocking(self._check_commit_config, source, directory, provider)
                self._check_cancelled(source_id)
                return self._state(
                    directory, source_id, "completed", **report, noop=True, **cleanup
                )
            previous_id = self._current(directory)
            previous_meta = await _blocking(self._metadata, directory, previous_id)
            if previous_meta is None:
                info = (await self.store.info()).wikis.get(source_id)
                if info and info.chunks:
                    raise SourceImportError("该来源已有不受此导入器管理的数据，拒绝覆盖")
            if provider is None:
                raise SourceImportError("嵌入未启用，不能将纯关键词导入伪报成功")
            if previous_meta and previous_meta["fingerprint"] != provider.fingerprint:
                raise SourceImportError("嵌入指纹已变化，拒绝重嵌或混合旧来源向量")
            await _blocking(self._check_disk, directory, report["capacity"], previous_id)
            self._check_cancelled(source_id)
            previous = await _blocking(self._snapshot, directory, previous_id)
            self._check_cancelled(source_id)
            generation = uuid.uuid4().hex
            stage = self._generation(directory, generation)
            await _blocking(self._prepare_stage, stage, chunks, previous, provider)
            del previous
            self._check_cancelled(source_id)
            self._state(directory, source_id, "embedding", **report)
            await self._embed(
                provider, stage / "chunks.jsonl", stage / "vectors", stage / "verify.jsonl"
            )
            self._check_cancelled(source_id)
            records = await _blocking(
                self._write_snapshot, stage, rows, chunks, provider, source_id
            )
            self._check_cancelled(source_id)
            await _blocking(self._check_disk, directory, report["capacity"], previous_id, True)
            await _blocking(self._check_commit_config, source, directory, provider)
            self._check_cancelled(source_id)
            self._state(directory, source_id, "committing", generation=generation)
            _atomic_json(
                directory / "journal.json",
                {
                    "previous": previous_id,
                    "candidate": generation,
                    "fingerprint": provider.fingerprint,
                },
            )
            committing = True
            commit = asyncio.create_task(
                self.store.sync(
                    records,
                    fingerprint=provider.fingerprint,
                    wiki=source_id,
                )
            )
            try:
                synced = await asyncio.shield(commit)
            except asyncio.CancelledError:
                await commit
                raise
            try:
                _atomic_json(
                    directory / "current.json", {"generation": generation, "previous": previous_id}
                )
            except OSError:
                published = self._current(directory) == generation
                raise
            published = True
            (directory / "journal.json").unlink()
            cleanup = await _blocking(self._gc, directory)
            return self._state(
                directory,
                source_id,
                "completed",
                **report,
                noop=False,
                sync=asdict(synced),
                **cleanup,
            )
        except (Exception, asyncio.CancelledError) as exc:
            cancelled = isinstance(exc, (_Cancelled, asyncio.CancelledError))
            if committing and not published:
                try:
                    await asyncio.shield(self._restore(directory, source_id))
                except BaseException:
                    self._state(
                        directory,
                        source_id,
                        "recovery_required",
                        retryable=True,
                        error="提交恢复未完成；旧快照已保留，下次导入将先恢复",
                    )
                    raise SourceImportError("提交恢复未完成，需要重试恢复") from None
            if published:
                raise SourceImportPublishedError() from None
            cleanup = await _blocking(self._gc, directory)
            capacity_error = exc if isinstance(exc, SourceImportCapacityError) else None
            self._state(
                directory,
                source_id,
                "cancelled" if cancelled else "failed",
                retryable=True,
                **cleanup,
                error=capacity_error.public_message
                if capacity_error is not None
                else "导入已取消"
                if cancelled
                else "导入失败，旧资料保持不变",
                error_code=capacity_error.code if capacity_error is not None else None,
            )
            if cancelled:
                if isinstance(exc, asyncio.CancelledError):
                    raise
                return dict(self._states[source_id])
            if isinstance(exc, SourceImportCapacityError):
                raise exc
            if isinstance(exc, SourceImportError):
                raise SourceImportError(str(exc)) from None
            raise SourceImportError("导入失败，旧资料保持不变；请检查服务日志后重试") from None
        finally:
            self._tasks.pop(source_id, None)
            self._cancelled.discard(source_id)
            lock.close()

    async def aclose(self) -> None:
        self._closed = True
        for source_id in tuple(self._tasks):
            self.cancel(source_id)
        current = asyncio.current_task()
        tasks = [task for task in self._tasks.values() if task is not current]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
