"""Bounded stream-v2 archives: immutable delta manifests and shared float32 shards.

A generation contains only changed documents. Parent manifests remain immutable; readers
merge them with a disk-backed seen set, never collecting the complete history in Python.
Only the affected document scope is published or restored in PostgreSQL.
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import math
import os
import shutil
import sqlite3
import tempfile
import uuid
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jianshanagent.core.config import AppConfig
from jianshanagent.core.embedding import EmbeddingProvider
from jianshanagent.core.sources import load_source_adapter, source_instance
from jianshanagent.plugins.knowledge.chunker import ChunkStats, chunk_document
from jianshanagent.plugins.knowledge.embed import INDEX_FILE, VECTORS_FILE, text_hash
from jianshanagent.plugins.knowledge.settings import ChunkSettings, kb_root
from jianshanagent.plugins.knowledge.source_import import (
    SourceImportCapacityError,
    SourceImportError,
    SourceImportPublishedError,
    _atomic_json,
    _blocking,
    _Cancelled,
    _json_bytes,
    _now,
    _read_json,
    parse_jsonl,
)
from jianshanagent.plugins.knowledge.source_import_v2_io import (
    read_row,
    read_vector,
    stream_v1_snapshot,
    write_rows,
    write_vector_shard,
)
from jianshanagent.plugins.knowledge.store import KbChunk, KnowledgeStore, Record, scoped

if TYPE_CHECKING:
    from jianshanagent.plugins.knowledge.source_import import SourceImportService


@dataclass(frozen=True, slots=True)
class StreamImportLimits:
    max_documents: int = 2_000_000
    max_chunks: int = 2_000_000
    max_vector_bytes: int = 512 * 1024 * 1024
    max_archive_bytes: int = 1024 * 1024 * 1024
    max_metadata_bytes: int = 512 * 1024 * 1024
    max_disk_bytes: int = 4 * 1024 * 1024 * 1024
    max_working_bytes: int = 256 * 1024 * 1024
    disk_safety_bytes: int = 3 * 1024 * 1024 * 1024
    max_batch_documents: int = 256
    max_batch_chunks: int = 1024
    max_generations: int = 100_000

    def __post_init__(self) -> None:
        for item in fields(self):
            if type(getattr(self, item.name)) is not int or getattr(self, item.name) <= 0:
                raise ValueError(f"StreamImportLimits.{item.name} 必须为正整数")


def _directory(config: AppConfig, source_id: str) -> Path:
    # Reuse the namespace/path checks without constructing a service or store.
    import re

    source = source_instance(config, source_id)
    if source.driver != "documents" or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", source_id):
        raise SourceImportError("非法标准文档来源")
    directory = kb_root(config) / source_id / "source-import"
    for path in (kb_root(config), directory.parent, directory, directory / "generations"):
        if path.is_symlink():
            raise SourceImportError("来源目录不能是符号链接")
    return directory


def _mkdir_durable(path: Path) -> None:
    """Persist new namespace ancestors before any pointer/journal can become visible."""
    missing: list[Path] = []
    cursor = path
    while not cursor.exists():
        if cursor.is_symlink():
            raise SourceImportError("来源目录不能是符号链接")
        missing.append(cursor)
        cursor = cursor.parent
    if cursor.is_symlink():
        raise SourceImportError("来源目录不能是符号链接")
    path.mkdir(parents=True, exist_ok=True)
    for directory in (*missing, cursor):
        fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _generation(directory: Path, generation: str) -> Path:
    import re

    if not isinstance(generation, str) or not re.fullmatch(r"[0-9a-f]{32}", generation):
        raise SourceImportError("来源世代记录损坏")
    path = directory / "generations" / generation
    if path.is_symlink() or path.parent.is_symlink():
        raise SourceImportError("来源世代不能是符号链接")
    return path


@contextmanager
def _manifest(path: Path) -> Iterator[sqlite3.Connection]:
    if path.is_symlink() or not path.is_file():
        raise SourceImportError("来源清单不可用")
    db = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)
    try:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA cache_size=-1024")
        db.execute("PRAGMA query_only=ON")
        yield db
    finally:
        db.close()


def _chain(directory: Path, generation: str | None) -> Iterator[tuple[str, Path]]:
    # A hard bound rejects corrupted cycles without an unbounded Python visited set.
    for _ in range(100_000):
        if generation is None:
            return
        stage = _generation(directory, generation)
        manifest = stage / "manifest.sqlite"
        with _manifest(manifest) as db:
            meta = json.loads(
                db.execute("SELECT value FROM meta WHERE key='summary'").fetchone()[0]
            )
        yield generation, stage
        generation = meta.get("parent")
    raise SourceImportCapacityError("metadata_bytes")


def _summary(directory: Path, generation: str | None) -> dict[str, Any]:
    if generation is None:
        return {"documents": 0, "chunks": 0, "parent": None}
    with _manifest(_generation(directory, generation) / "manifest.sqlite") as db:
        result: dict[str, Any] = json.loads(
            db.execute("SELECT value FROM meta WHERE key='summary'").fetchone()[0]
        )
    return result


def _entry(
    directory: Path, generation: str | None, row_id: str, *, with_chunks: bool = True
) -> dict[str, Any] | None:
    for _, stage in _chain(directory, generation):
        with _manifest(stage / "manifest.sqlite") as db:
            columns = {column[1] for column in db.execute("PRAGMA table_info(documents)")}
            deleted_column = "deleted" if "deleted" in columns else "0 AS deleted"
            selection = "*" if with_chunks else "id,raw_offset,raw_length," + deleted_column
            found = db.execute(
                "SELECT " + selection + " FROM documents WHERE id=?", (row_id,)
            ).fetchone()
            if found is not None:
                result = dict(found)
                if result.get("deleted", 0):
                    return None
                result.setdefault("logical_id", str(row_id) if ":" in str(row_id) else "")
                result["stage"] = stage
                if with_chunks:
                    result["chunks"] = json.loads(result["chunks"])
                return result
    return None


def _logical_entries(
    directory: Path, generation: str | None, packet_id: str
) -> dict[str, dict[str, Any]]:
    entries: dict[str, dict[str, Any]] = {}
    seen: set[str] = set()
    for _, stage in _chain(directory, generation):
        with _manifest(stage / "manifest.sqlite") as db:
            columns = {column[1] for column in db.execute("PRAGMA table_info(documents)")}
            if "logical_id" in columns:
                query = (
                    "SELECT id,doc_key,raw_offset,raw_length,deleted,logical_id,"
                    "json_array_length(chunks) AS chunk_count FROM documents WHERE logical_id=?"
                )
            else:
                # Pre-fragment V2 manifests always had a single physical row per logical ID.
                query = (
                    "SELECT id,doc_key,raw_offset,raw_length,0 AS deleted,id AS logical_id,"
                    "json_array_length(chunks) AS chunk_count FROM documents WHERE id=?"
                )
            for found in db.execute(query, (packet_id,)):
                if found["id"] in seen:
                    continue
                seen.add(found["id"])
                if len(seen) > 64:
                    raise SourceImportCapacityError("metadata_bytes")
                if found["deleted"]:
                    continue
                item = dict(found)
                item["stage"] = stage
                entries[item["id"]] = item
    return entries


def _node_owner(directory: Path, generation: str | None, node_id: str) -> str | None:
    for _, stage in _chain(directory, generation):
        with _manifest(stage / "manifest.sqlite") as db:
            found = db.execute("SELECT packet FROM nodes WHERE id=?", (node_id,)).fetchone()
            if found is not None:
                return str(found[0])
    return None


def _entries(directory: Path, generation: str | None) -> Iterator[dict[str, Any]]:
    # SQLite's disk-backed primary key deduplication bounds RSS independently of history.
    with tempfile.TemporaryDirectory(prefix="source-v2-reader-") as scratch:
        seen = sqlite3.connect(str(Path(scratch) / "seen.sqlite"))
        try:
            seen.execute("PRAGMA cache_size=-1024")
            seen.execute("CREATE TABLE seen(id TEXT PRIMARY KEY) WITHOUT ROWID")
            for _, stage in _chain(directory, generation):
                with _manifest(stage / "manifest.sqlite") as db:
                    columns = {column[1] for column in db.execute("PRAGMA table_info(documents)")}
                    deleted_column = "deleted" if "deleted" in columns else "0 AS deleted"
                    for record in db.execute(
                        "SELECT id,raw_offset,raw_length,"
                        + deleted_column
                        + " FROM documents ORDER BY id"
                    ):
                        if seen.execute(
                            "INSERT OR IGNORE INTO seen VALUES(?)", (record["id"],)
                        ).rowcount:
                            if record["deleted"]:
                                continue
                            row = dict(record)
                            row["stage"] = stage
                            yield row
                seen.commit()
        finally:
            seen.close()


def _raw(entry: dict[str, Any]) -> dict[str, Any]:
    return read_row(entry["stage"] / "rows.jsonl.gz", entry["raw_offset"], entry["raw_length"])


def iter_source_rows(config: AppConfig, source_id: str) -> Iterator[dict[str, Any]]:
    """Yield original V1/V2 rows without normalization, float snapshots or mutable opens.

    Writers retain the complete current ancestry during GC, so a concurrent reader's
    selected generation stays reachable through current/previous. No lease re-entry.
    """
    directory = _directory(config, source_id)
    pointer = directory / "current.json"
    if not pointer.exists():
        return
    current = _read_json(pointer)
    generation = current["generation"]
    if current.get("version") == 2:
        for entry in _entries(directory, generation):
            yield _raw(entry)
    else:
        for kind, row in stream_v1_snapshot(_generation(directory, generation) / "snapshot.json"):
            if kind == "row":
                yield row


def get_source_row(config: AppConfig, source_id: str, row_id: str) -> dict[str, Any] | None:
    """Read one persisted original row. Does not call an adapter (ownership-safe)."""
    directory = _directory(config, source_id)
    if not (directory / "current.json").exists():
        return None
    pointer = _read_json(directory / "current.json")
    if pointer.get("version") == 2:
        entry = _entry(directory, pointer["generation"], str(row_id), with_chunks=False)
        return _raw(entry) if entry is not None else None
    return next(
        (row for row in iter_source_rows(config, source_id) if str(row.get("id")) == str(row_id)),
        None,
    )


def uses_stream_v2(config: AppConfig, source_id: str) -> bool:
    if source_instance(config, source_id).raw.get("storage") == "stream_v2":
        return True
    directory = _directory(config, source_id)
    for name in ("current.json", "journal.json"):
        if (directory / name).exists() and _read_json(directory / name).get("version") == 2:
            return True
    return False


def _group_members(config: AppConfig, source_id: str) -> tuple[str, list[str]]:
    source = source_instance(config, source_id)
    group = source.raw.get("discussion_of") or source.id
    members = {group}
    for raw in config.files.get("core.sources", {}).get("sources", []):
        if raw.get("discussion_of") == group:
            members.add(raw["id"])
    return group, sorted(members)


def _resource_accounting(
    config: AppConfig, source_id: str, measured: dict[str, Any], trusted_probe: bool
) -> dict[str, Any]:
    import stat

    for key in ("database_bytes", "wal_position_bytes", "pg_started_at"):
        if type(measured.get(key)) is not int or measured[key] < 0:
            raise SourceImportCapacityError("resource_measurement")
    root = kb_root(config)
    group, _ = _group_members(config, source_id)
    directory = _directory(config, group)
    _mkdir_durable(directory)
    verified = False
    free_bytes = 0
    if trusted_probe:
        free_bytes = measured.get("pg_disk_free_bytes", -1)
        verified = type(free_bytes) is int and free_bytes >= 0
    else:
        proof_path = root / ".source-storage-volume.json"
        try:
            info = proof_path.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_uid not in {0, os.geteuid()}
            ):
                raise SourceImportCapacityError("resource_measurement")
            proof = _read_json(proof_path, 4096)
            if (
                not isinstance(proof, dict)
                or set(proof) != {"kb_device", "pg_device", "pg_started_at"}
                or any(type(value) is not int for value in proof.values())
                or proof["kb_device"] != proof["pg_device"]
                or proof["kb_device"] != root.stat().st_dev
                or proof["pg_started_at"] != measured["pg_started_at"]
            ):
                raise SourceImportCapacityError("resource_measurement")
            free_bytes = shutil.disk_usage(root).free
            verified = True
        except (OSError, ValueError, TypeError, KeyError):
            raise SourceImportCapacityError("resource_measurement") from None
    if not verified:
        raise SourceImportCapacityError("resource_measurement")
    lock_path = directory / "resource-baseline.lock"
    if lock_path.is_symlink():
        raise SourceImportCapacityError("resource_measurement")
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        path = directory / "resource-baseline-v2.json"
        if path.exists():
            baseline = _read_json(path)
            if any(
                type(baseline.get(key)) is not int
                for key in ("database_bytes", "wal_position_bytes")
            ):
                raise SourceImportCapacityError("resource_measurement")
            if measured["wal_position_bytes"] < baseline["wal_position_bytes"]:
                raise SourceImportCapacityError("resource_measurement")
        else:
            baseline = {key: measured[key] for key in ("database_bytes", "wal_position_bytes")}
            _atomic_json(path, baseline)
    finally:
        os.close(fd)
    growth = max(0, measured["database_bytes"] - baseline["database_bytes"]) + max(
        0, measured["wal_position_bytes"] - baseline["wal_position_bytes"]
    )
    return {
        **measured,
        "pg_growth_bytes": growth,
        "pg_free_verified": verified,
        "pg_disk_free_bytes": free_bytes,
    }


async def measure_stream_resources(
    config: AppConfig,
    store: KnowledgeStore,
    source_id: str,
    *,
    resource_probe: Callable[[list[str]], Awaitable[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Shared fail-closed PG growth/free-space accounting for every V2 write entry.

    The fixed owner-only proof is written by deployment after inspecting both physical
    mounts. Its postmaster epoch-microseconds expires on PG restart. Request data cannot
    supply the proof or a probe; the optional callback is only a trusted host/test seam.
    """
    _, members = _group_members(config, source_id)
    try:
        measured = await (
            resource_probe(members) if resource_probe else store.resource_usage(members)
        )
        result: dict[str, Any] = await _blocking(
            _resource_accounting, config, source_id, measured, resource_probe is not None
        )
        return result
    except asyncio.CancelledError:
        raise
    except Exception:
        raise SourceImportCapacityError("resource_measurement") from None


def _digest(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def _new_manifest(stage: Path) -> sqlite3.Connection:
    db = sqlite3.connect(str(stage / "manifest.sqlite"))
    db.execute("PRAGMA journal_mode=DELETE")
    db.execute("PRAGMA synchronous=FULL")
    db.execute("CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
    db.execute("CREATE TABLE nodes(id TEXT PRIMARY KEY,packet TEXT NOT NULL)")
    db.execute(
        "CREATE TABLE documents(id TEXT PRIMARY KEY,doc_key TEXT UNIQUE NOT NULL,"
        "digest TEXT NOT NULL,raw_offset INTEGER NOT NULL,raw_length INTEGER NOT NULL,"
        "chunks TEXT NOT NULL,logical_id TEXT NOT NULL,deleted INTEGER NOT NULL DEFAULT 0)"
    )
    db.execute("CREATE INDEX documents_logical ON documents(logical_id)")
    return db


def _seal_manifest(db: sqlite3.Connection, stage: Path, summary: dict[str, Any]) -> None:
    db.execute("INSERT INTO meta VALUES('summary',?)", (json.dumps(summary),))
    db.commit()
    db.close()
    with (stage / "manifest.sqlite").open("rb") as file:
        os.fsync(file.fileno())
    # Persist both the vector-file entries and the newly created generation entry.
    # fsync(source-import) when publishing current is not a substitute for generations.
    for directory in (stage / "vectors", stage, stage.parent):
        if not directory.exists():
            continue
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


class StreamSourceImporter:
    def __init__(self, service: SourceImportService):
        self.service = service
        self.limits: StreamImportLimits = service.v2_limits

    def _validate(
        self, source_id: str, content: str | bytes
    ) -> tuple[list[dict[str, Any]], list[Any]]:
        raw_bytes = len(content.encode() if isinstance(content, str) else content)
        if raw_bytes * 12 > self.limits.max_working_bytes:
            raise SourceImportCapacityError("working_bytes")
        rows = parse_jsonl(content)
        if len(rows) > self.limits.max_batch_documents:
            raise SourceImportCapacityError("documents")
        source = source_instance(self.service.config, source_id)
        if source.platform.id != "weibo":
            raise SourceImportError("V2 存储仅用于微博文档")
        try:
            docs = load_source_adapter(self.service.config, source_id).normalize(rows)
        except ValueError as exc:
            raise SourceImportError(str(exc)) from None
        self.service._check_authors(source_id, rows, docs)
        parent_id = source.raw.get("discussion_of")
        if parent_id:
            parent = source_instance(self.service.config, parent_id)
            uid = (parent.raw.get("collector") or {}).get("uid")
            if not uid or source.raw.get("role", "community") != "community":
                raise SourceImportError("讨论来源必须绑定有效父来源并保持 community 身份")
            for row in rows:
                persisted = get_source_row(self.service.config, parent_id, str(row.get("post_id")))
                if (
                    row.get("version") != 2
                    or row.get("post_author_id") != uid
                    or persisted is None
                    or persisted.get("author_id") != uid
                ):
                    raise SourceImportError("讨论所属微博必须由已持久化父来源证明作者归属")
        elif any(row.get("version") != 1 for row in rows):
            raise SourceImportError("正文来源仅接受 version=1 记录")
        return rows, docs

    def _discussion_replacements(
        self, directory: Path, generation: str | None, rows: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], dict[str, dict[str, str]]]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            if row.get("version") == 2:
                grouped.setdefault(str(row.get("packet_id", row["id"])), []).append(row)
        removed: list[dict[str, Any]] = []
        scopes: dict[str, dict[str, str]] = {}
        for packet_id, fragments in grouped.items():
            previous = _logical_entries(directory, generation, packet_id)
            old_nodes = {
                str(node["id"]) for entry in previous.values() for node in _raw(entry)["nodes"]
            }
            new_nodes = [str(node["id"]) for fragment in fragments for node in fragment["nodes"]]
            if len(new_nodes) > 64 or len(new_nodes) != len(set(new_nodes)):
                raise SourceImportError("逻辑讨论包必须保留至多64个不重复节点")
            if not old_nodes.issubset(new_nodes):
                raise SourceImportError("必须一次发布逻辑讨论包的全部片段，不能遗漏既有节点")
            incoming_ids = {str(fragment["id"]) for fragment in fragments}
            scope = {key: item["doc_key"] for key, item in previous.items()}
            scope.update({row_id: f"weibo:discussion:{row_id}" for row_id in incoming_ids})
            scopes[packet_id] = scope
            if sum(len(value) for value in scopes.values()) > self.limits.max_batch_documents:
                raise SourceImportCapacityError("documents")
            for row_id in previous.keys() - incoming_ids:
                old = previous[row_id]
                removed.append(
                    {
                        "row": {"id": row_id},
                        "doc_key": old["doc_key"],
                        "digest": "deleted",
                        "chunks": [],
                        "old": old,
                        "deleted": True,
                        "logical_id": packet_id,
                        "scope": scope,
                    }
                )
        return removed, scopes

    def candidate(
        self, source_id: str, content: str | bytes
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:

        directory = self.service._directory(source_id)
        if (directory / "journal.json").exists():
            raise SourceImportError("上次导入待恢复；请重新确认导入以恢复后重试")
        rows, docs = self._validate(source_id, content)
        pointer = (
            _read_json(directory / "current.json") if (directory / "current.json").exists() else {}
        )
        generation = pointer.get("generation") if pointer.get("version") == 2 else None
        summary = _summary(directory, generation)
        # V1 preview reads only requested originals; migration happens under the import lock.
        old_v1: dict[str, dict[str, Any]] = {}
        legacy_chunk_counts: dict[str, int] = {}
        if pointer and generation is None:
            wanted = {str(row["id"]) for row in rows}
            wanted_docs = {doc.doc_key for doc in docs}
            for kind, legacy in stream_v1_snapshot(
                _generation(directory, pointer["generation"]) / "snapshot.json"
            ):
                if kind == "row":
                    summary["documents"] += 1
                    if str(legacy["id"]) in wanted:
                        old_v1[str(legacy["id"])] = legacy
                elif kind == "record":
                    summary["chunks"] += 1
                    key = legacy["chunk"]["doc_key"]
                    if key in wanted_docs:
                        legacy_chunk_counts[key] = legacy_chunk_counts.get(key, 0) + 1
        changed, logical_scopes = self._discussion_replacements(directory, generation, rows)
        deleted = len(changed)
        removed_chunks = sum(item["old"]["chunk_count"] for item in changed)
        new = updated = unchanged = added_chunks = batch_chunks = 0
        for row, doc in zip(rows, docs, strict=True):
            logical_id = str(row.get("packet_id", row["id"])) if row.get("version") == 2 else ""
            for node in row.get("nodes", []):
                owner = _node_owner(directory, generation, str(node["id"]))
                if owner is not None and owner != logical_id:
                    raise SourceImportError("原生讨论节点已分配到其他稳定讨论包")
            old = _entry(directory, generation, str(row["id"]))
            chunks = [
                chunk.to_json() for chunk in chunk_document(doc, ChunkSettings(), ChunkStats())
            ]
            batch_chunks += len(chunks)
            if batch_chunks > self.limits.max_batch_chunks:
                raise SourceImportCapacityError("chunks")
            digest = _digest({"row": row, "chunks": chunks})
            if old is not None and old["digest"] == digest:
                unchanged += 1
                continue
            if old is None and str(row["id"]) not in old_v1:
                new += 1
            elif old is None and old_v1[str(row["id"])] == row:
                unchanged += 1
                continue
            else:
                updated += 1
            added_chunks += len(chunks)
            removed_chunks += len(old["chunks"]) if old else legacy_chunk_counts.get(doc.doc_key, 0)
            changed.append(
                {
                    "row": row,
                    "doc_key": doc.doc_key,
                    "digest": digest,
                    "chunks": chunks,
                    "old": old,
                    "logical_id": logical_id,
                    "deleted": False,
                    "scope": logical_scopes.get(logical_id, {str(row["id"]): doc.doc_key}),
                }
            )
        total = summary["documents"] + new - deleted
        chunk_total = summary["chunks"] + added_chunks - removed_chunks
        provider = self.service._embedding_provider(self.service.config)
        dimensions = provider.dimensions if provider else 4096
        metadata_bytes = sum(len(json.dumps(item, default=str).encode()) for item in changed)
        working = (
            len(content.encode() if isinstance(content, str) else content) * 8
            + metadata_bytes * 8
            + batch_chunks * dimensions * 16
        )
        for code, actual, maximum in (
            ("documents", total, self.limits.max_documents),
            ("chunks", chunk_total, self.limits.max_chunks),
            ("working_bytes", working, self.limits.max_working_bytes),
        ):
            if actual > maximum:
                raise SourceImportCapacityError(code)
        report = {
            "source_id": source_id,
            "records": len(rows),
            "new": new,
            "updated": updated,
            "deleted": deleted,
            "unchanged": unchanged,
            "total_documents": total,
            "chunks": chunk_total,
            "empty": not rows,
            "storage": "stream_v2",
            "capacity": {
                "documents": total,
                "chunks": chunk_total,
                "dimensions": dimensions,
                "vector_bytes": chunk_total * dimensions * 4,
                "working_bytes": working,
                "generation_bytes": metadata_bytes * 3 + batch_chunks * dimensions * 4 + 65536,
            },
            "sample": [
                {
                    "id": row.get("id"),
                    "title": doc.title,
                    "text": "\n\n".join(b.render() for b in doc.blocks)[:1000],
                    "url": doc.url,
                    "author_id": doc.meta.get("author_id"),
                    "published_at": doc.meta.get("published_at"),
                }
                for row, doc in zip(rows[:5], docs[:5], strict=True)
            ],
        }
        return report, changed

    async def _measure_capacity(self, directory: Path, reserve: int = 0) -> dict[str, Any]:
        measured = await measure_stream_resources(
            self.service.config,
            self.service.store,
            directory.parent.name,
            resource_probe=self.service.resource_probe,
        )
        if measured["pg_disk_free_bytes"] < self.limits.disk_safety_bytes + reserve:
            raise SourceImportCapacityError("disk_free_bytes")
        usage = await _blocking(self._capacity, directory, reserve, measured["pg_growth_bytes"])
        return {**usage, **measured}

    def _capacity(self, directory: Path, reserve: int = 0, pg_growth: int = 0) -> dict[str, int]:
        usage = {"archive_bytes": 0, "metadata_bytes": 0, "vector_bytes": 0, "resource_bytes": 0}
        # Group budget includes the parent/child archive and the runner's durable queues.
        root = directory.parent.parent
        source = source_instance(self.service.config, directory.parent.name)
        group = source.raw.get("discussion_of") or source.id
        members = {group}
        for raw in self.service.config.files.get("core.sources", {}).get("sources", []):
            if raw.get("discussion_of") == group:
                members.add(raw["id"])
        for member in members:
            member_root = root / member
            if member_root.is_symlink():
                raise SourceImportError("来源目录不能是符号链接")
            for current, dirs, files in os.walk(member_root, followlinks=False):
                if any((Path(current) / name).is_symlink() for name in dirs):
                    raise SourceImportError("来源目录不能是符号链接")
                for name in files:
                    path = Path(current) / name
                    if path.is_symlink():
                        raise SourceImportError("来源文件不能是符号链接")
                    size = path.stat().st_size
                    usage["resource_bytes"] += size
                    if name.endswith(".f32"):
                        usage["vector_bytes"] += size
                    elif name.endswith(".gz"):
                        usage["archive_bytes"] += size
                    elif name.endswith((".sqlite", ".sqlite-wal", ".sqlite-shm")):
                        usage["metadata_bytes"] += size
        for code, actual, maximum in (
            ("vector_bytes", usage["vector_bytes"] + reserve, self.limits.max_vector_bytes),
            ("snapshot_bytes", usage["archive_bytes"] + reserve, self.limits.max_archive_bytes),
            ("metadata_bytes", usage["metadata_bytes"] + reserve, self.limits.max_metadata_bytes),
            (
                "snapshot_bytes",
                usage["resource_bytes"] + pg_growth + reserve,
                self.limits.max_disk_bytes,
            ),
        ):
            if actual > maximum:
                raise SourceImportCapacityError(code)
        if shutil.disk_usage(directory).free < self.limits.disk_safety_bytes + reserve:
            raise SourceImportCapacityError("disk_free_bytes")
        usage["local_resource_bytes"] = usage["resource_bytes"]
        usage["resource_bytes"] += pg_growth
        return usage

    def _prepare(self, stage: Path, changed: list[dict[str, Any]]) -> None:
        stage.mkdir(parents=True)
        locations = write_rows(
            stage / "rows.jsonl.gz", (item["row"] for item in changed if not item.get("deleted"))
        )
        with (stage / "chunks.jsonl").open("x", encoding="utf-8") as output:
            for item in changed:
                item["raw_offset"], item["raw_length"] = locations.get(
                    str(item["row"]["id"]), (0, 0)
                )
                reusable = {
                    (c["chunk"]["chunk_id"], c["chunk"]["text"]): c["vector"]
                    for c in (
                        item["old"]["chunks"] if item["old"] and not item.get("deleted") else []
                    )
                }
                item["sealed_chunks"] = []
                for chunk in item["chunks"]:
                    reference = reusable.get((chunk["chunk_id"], chunk["text"]))
                    item["sealed_chunks"].append({"chunk": chunk, "vector": reference})
                    if reference is None:
                        output.write(json.dumps(chunk, ensure_ascii=False) + "\n")
            output.flush()
            os.fsync(output.fileno())

    def _seal(
        self,
        stage: Path,
        changed: list[dict[str, Any]],
        provider: EmbeddingProvider,
        summary: dict[str, Any],
    ) -> None:
        locations: dict[tuple[str, str], int] = {}
        index = stage / "vectors" / INDEX_FILE
        if index.exists():
            with index.open(encoding="utf-8") as lines:
                for number, line in enumerate(lines):
                    record = json.loads(line)
                    locations[(record["chunk_id"], record["text_hash"])] = (
                        number * provider.dimensions * 4
                    )
        db = _new_manifest(stage)
        try:
            for item in changed:
                for node in item["row"].get("nodes", []):
                    db.execute(
                        "INSERT INTO nodes VALUES(?,?)", (str(node["id"]), item["logical_id"])
                    )
                for chunk in item["sealed_chunks"]:
                    if chunk["vector"] is None:
                        row = chunk["chunk"]
                        offset = locations.get((row["chunk_id"], text_hash(row["text"])))
                        if offset is None:
                            raise SourceImportError("候选向量不完整，旧资料保持不变")
                        chunk["vector"] = {
                            "generation": stage.name,
                            "path": "vectors/" + VECTORS_FILE,
                            "offset": offset,
                            "length": provider.dimensions * 4,
                        }
                    self._vector(stage.parent.parent, chunk["vector"], provider.dimensions)
                db.execute(
                    "INSERT INTO documents VALUES(?,?,?,?,?,?,?,?)",
                    (
                        str(item["row"]["id"]),
                        item["doc_key"],
                        item["digest"],
                        item["raw_offset"],
                        item["raw_length"],
                        json.dumps(item["sealed_chunks"], ensure_ascii=False),
                        item["logical_id"],
                        int(item.get("deleted", False)),
                    ),
                )
            _seal_manifest(db, stage, summary)
        except BaseException:
            db.close()
            raise

    @staticmethod
    def _vector(directory: Path, ref: dict[str, Any], dimensions: int) -> Any:
        if ref["path"] not in {"vectors/" + VECTORS_FILE, "migrated.f32"}:
            raise SourceImportError("非法向量分片引用")
        stage = _generation(directory, ref["generation"])
        path = stage / ref["path"]
        if path.parent.is_symlink():
            raise SourceImportError("向量目录不能是符号链接")
        vector = read_vector(path, ref["offset"], ref["length"], dimensions)
        if not all(math.isfinite(value) for value in vector):
            raise SourceImportError("候选向量数值无效")
        return vector

    def _records(
        self,
        directory: Path,
        generation: str | None,
        source_id: str,
        keys: list[str],
        dimensions: int,
    ) -> Iterator[Record]:
        # The journal keeps row IDs, not an unbounded list of all historical docs.
        for row_id in keys:
            entry = _entry(directory, generation, row_id)
            if entry is not None:
                for chunk in entry["chunks"]:
                    yield (
                        KbChunk.from_json(chunk["chunk"], source_id),
                        self._vector(directory, chunk["vector"], dimensions),
                    )

    def _migrate(self, directory: Path, source_id: str, provider: EmbeddingProvider) -> None:
        pointer_path = directory / "current.json"
        if not pointer_path.exists():
            return
        pointer = _read_json(pointer_path)
        if pointer.get("version") == 2:
            return
        source = source_instance(self.service.config, source_id)
        original = _generation(directory, pointer["generation"]) / "snapshot.json"
        generation = uuid.uuid4().hex
        stage = _generation(directory, generation)
        self._capacity(directory, original.stat().st_size * 2 + 65536)
        stage.mkdir(parents=True)
        db = _new_manifest(stage)
        documents = chunks = 0
        fingerprint: str | None = None

        # Raw rows and records are each decoded one at a time, including legacy float JSON.
        def legacy_rows() -> Iterator[dict[str, Any]]:
            count = 0
            for kind, row in stream_v1_snapshot(original):
                if kind == "row":
                    count += 1
                    if count > self.service.limits.max_documents:
                        raise SourceImportCapacityError("documents")
                    yield row

        positions = write_rows(stage / "rows.jsonl.gz", legacy_rows())
        adapter = load_source_adapter(self.service.config, source_id)
        for kind, row in stream_v1_snapshot(original):
            if kind == "meta":
                fingerprint = row.get("fingerprint")
            elif kind == "row":
                doc = adapter.normalize([row])[0]
                self.service._check_authors(source_id, [row], [doc])
                offset, length = positions[str(row["id"])]
                db.execute(
                    "INSERT INTO documents VALUES(?,?,?,?,?,?,?,?)",
                    (
                        str(row["id"]),
                        doc.doc_key,
                        "pending",
                        offset,
                        length,
                        "[]",
                        "",
                        0,
                    ),
                )
                documents += 1
        if fingerprint != provider.fingerprint:
            db.close()
            raise SourceImportError("嵌入指纹已变化，拒绝迁移或混合旧向量")

        def vectors() -> Iterator[tuple[str, Any]]:
            nonlocal chunks
            for kind, record in stream_v1_snapshot(original):
                if kind != "record":
                    continue
                chunk = record["chunk"]
                if chunks >= self.service.limits.max_chunks:
                    raise SourceImportCapacityError("chunks")
                number = chunks
                chunks += 1
                reference = {
                    "generation": generation,
                    "path": "migrated.f32",
                    "offset": number * provider.dimensions * 4,
                    "length": provider.dimensions * 4,
                }
                previous = db.execute(
                    "SELECT id,chunks FROM documents WHERE doc_key=?", (chunk["doc_key"],)
                ).fetchone()
                if previous is None:
                    raise SourceImportError("V1 快照文档向量归属损坏")
                parts = json.loads(previous[1])
                parts.append({"chunk": chunk, "vector": reference})
                db.execute(
                    "UPDATE documents SET chunks=? WHERE id=?",
                    (json.dumps(parts, ensure_ascii=False), previous[0]),
                )
                yield str(number), record["vector"]

        try:
            write_vector_shard(stage / "migrated.f32", vectors(), provider.dimensions)
            for row_id, offset, length, serialized in db.execute(
                "SELECT id,raw_offset,raw_length,chunks FROM documents ORDER BY id"
            ):
                original_row = read_row(stage / "rows.jsonl.gz", offset, length)
                original_chunks = [part["chunk"] for part in json.loads(serialized)]
                db.execute(
                    "UPDATE documents SET digest=? WHERE id=?",
                    (
                        _digest({"row": original_row, "chunks": original_chunks}),
                        row_id,
                    ),
                )
            _seal_manifest(
                db,
                stage,
                {
                    "documents": documents,
                    "chunks": chunks,
                    "parent": None,
                    "fingerprint": provider.fingerprint,
                    "dimensions": provider.dimensions,
                    "version": 2,
                    "depth": 1,
                    "migration_previous": pointer["generation"],
                },
            )
        except BaseException:
            db.close()
            raise
        self._capacity(directory)
        self.service._check_commit_config(source, directory, provider)
        self.service._check_cancelled(source_id)
        _atomic_json(
            pointer_path,
            {
                "version": 2,
                "generation": generation,
                "previous": pointer["generation"],
                "legacy_previous": pointer["generation"],
            },
        )

    async def restore(self, directory: Path, source_id: str) -> None:
        journal = _read_json(directory / "journal.json")
        current = self.service._current(directory)
        summary = _summary(directory, current)
        await self.service.store.sync_documents(
            self._records(directory, current, source_id, journal["row_ids"], journal["dimensions"]),
            fingerprint=summary.get("fingerprint", journal["fingerprint"]),
            wiki=source_id,
            doc_keys=journal["doc_keys"],
        )
        (directory / "journal.json").unlink()

    def gc(self, directory: Path) -> dict[str, Any]:
        try:
            if (directory / "journal.json").exists():
                return {"cleanup_pending": True}
            pointer = (
                _read_json(directory / "current.json")
                if (directory / "current.json").exists()
                else {}
            )
            keep: set[str] = set()
            for key in ("generation", "previous", "legacy_previous"):
                generation = pointer.get(key)
                if generation is None:
                    continue
                if (_generation(directory, generation) / "manifest.sqlite").exists():
                    for current, _ in _chain(directory, generation):
                        keep.add(current)
                        legacy = _summary(directory, current).get("migration_previous")
                        if legacy:
                            keep.add(legacy)
                else:
                    keep.add(generation)
            root = directory / "generations"
            if root.exists():
                for path in root.iterdir():
                    if path.name not in keep and not path.is_symlink() and path.is_dir():
                        shutil.rmtree(path)
            return {"cleanup_pending": False}
        except (OSError, ValueError, KeyError, sqlite3.Error):
            return {"cleanup_pending": True}

    async def import_data(self, source_id: str, content: str | bytes) -> dict[str, Any]:
        service = self.service
        if service._closed or source_id in service._tasks:
            raise SourceImportError("导入服务已关闭或该来源已有导入任务")
        directory = service._directory(source_id)
        source = source_instance(service.config, source_id)
        if not source.mounted:
            raise SourceImportError("来源已停用，请恢复后导入")
        startup_provider: EmbeddingProvider | Exception | None
        try:
            startup_provider = service._embedding_provider(service.config)
        except Exception as exc:
            startup_provider = exc
        await _blocking(self._validate, source_id, content)
        if source_id in service._tasks:
            raise SourceImportError("该来源已有导入任务")
        await _blocking(_mkdir_durable, directory)
        lock = (directory / "import.lock").open("ab")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            lock.close()
            raise SourceImportError("该来源正由另一个导入任务处理") from None
        task = asyncio.current_task()
        assert task is not None
        service._tasks[source_id] = task
        service._cancelled.discard(source_id)
        service._states[source_id] = {"job_id": uuid.uuid4().hex, "started_at": _now()}
        committing = published = False
        try:
            if (directory / "journal.json").exists():
                journal = _read_json(directory / "journal.json")
                if journal.get("version") == 2:
                    await self.restore(directory, source_id)
                else:
                    await service._restore(directory, source_id)
            if isinstance(startup_provider, Exception):
                raise startup_provider
            provider = startup_provider
            if provider is None:
                raise SourceImportError("嵌入未启用，不能将纯关键词导入伪报成功")
            service._state(directory, source_id, "validating")
            await self._measure_capacity(directory)
            await _blocking(service._check_commit_config, source, directory, provider)
            service._check_cancelled(source_id)
            await _blocking(self._migrate, directory, source_id, provider)
            report, changed = await _blocking(self.candidate, source_id, content)
            previous = service._current(directory)
            old_summary = _summary(directory, previous)
            if previous and old_summary["fingerprint"] != provider.fingerprint:
                raise SourceImportError("嵌入指纹已变化，拒绝重嵌或混合旧来源向量")
            if not previous:
                info = (await service.store.info()).wikis.get(source_id)
                if info and info.chunks:
                    raise SourceImportError("该来源已有不受此导入器管理的数据，拒绝覆盖")
            service._check_cancelled(source_id)
            if not changed:
                await service.store.info()
                await _blocking(service._check_commit_config, source, directory, provider)
                service._check_cancelled(source_id)
                return service._state(
                    directory, source_id, "completed", **report, noop=True, **self.gc(directory)
                )
            if old_summary.get("depth", 0) >= self.limits.max_generations:
                raise SourceImportCapacityError("metadata_bytes")
            commit_reserve = max(18 * 1024 * 1024, report["capacity"]["generation_bytes"] * 4)
            await self._measure_capacity(directory, commit_reserve)
            generation = uuid.uuid4().hex
            stage = _generation(directory, generation)
            await _blocking(self._prepare, stage, changed)
            service._state(directory, source_id, "embedding", **report)
            service._check_cancelled(source_id)
            if (stage / "chunks.jsonl").stat().st_size:
                await service._embed(
                    provider, stage / "chunks.jsonl", stage / "vectors", stage / "chunks.jsonl"
                )
            service._check_cancelled(source_id)
            summary = {
                "version": 2,
                "parent": previous,
                "documents": report["total_documents"],
                "chunks": report["chunks"],
                "fingerprint": provider.fingerprint,
                "dimensions": provider.dimensions,
                "depth": old_summary.get("depth", 0) + 1,
            }
            await _blocking(self._seal, stage, changed, provider, summary)
            # Reserve PostgreSQL's vector/index/WAL copy plus rollback/control writes.
            # The runner additionally measures actual combined PostgreSQL growth.
            usage = await self._measure_capacity(directory, commit_reserve)
            await _blocking(service._check_commit_config, source, directory, provider)
            service._check_cancelled(source_id)
            scope: dict[str, str] = {}
            for item in changed:
                scope.update(item["scope"])
            keys = sorted(scope)
            doc_keys = [scoped(source_id, scope[key]) for key in keys]
            journal = {
                "version": 2,
                "previous": previous,
                "candidate": generation,
                "fingerprint": provider.fingerprint,
                "dimensions": provider.dimensions,
                "row_ids": keys,
                "doc_keys": doc_keys,
            }
            if _json_bytes(journal) > 64 * 1024:
                raise SourceImportCapacityError("metadata_bytes")
            service._state(directory, source_id, "committing", generation=generation)
            _atomic_json(directory / "journal.json", journal)
            committing = True

            async def commit() -> Any:
                result = await service.store.sync_documents(
                    self._records(directory, generation, source_id, keys, provider.dimensions),
                    fingerprint=provider.fingerprint,
                    wiki=source_id,
                    doc_keys=doc_keys,
                )
                _atomic_json(
                    directory / "current.json",
                    {"version": 2, "generation": generation, "previous": previous},
                )
                return result

            work = asyncio.create_task(commit())
            try:
                synced = await asyncio.shield(work)
            except asyncio.CancelledError:
                # Hold the file lock through repeated cancellation until PG+pointer finishes.
                while not work.done():
                    try:
                        await asyncio.shield(work)
                    except asyncio.CancelledError:
                        continue
                synced = work.result()
                published = service._current(directory) == generation
                if published:
                    (directory / "journal.json").unlink(missing_ok=True)
                raise
            published = True
            (directory / "journal.json").unlink()
            return service._state(
                directory,
                source_id,
                "completed",
                **report,
                **usage,
                noop=False,
                sync=asdict(synced),
                **self.gc(directory),
            )
        except (Exception, asyncio.CancelledError) as exc:
            if committing and not published:
                # Pointer replacement may have succeeded before fsync reported a failure.
                published = service._current(directory) == generation
            if committing and not published:
                recovery = asyncio.create_task(self.restore(directory, source_id))
                try:
                    while not recovery.done():
                        try:
                            await asyncio.shield(recovery)
                        except asyncio.CancelledError:
                            continue
                    recovery.result()
                except BaseException:
                    service._state(
                        directory,
                        source_id,
                        "recovery_required",
                        retryable=True,
                        error="提交恢复未完成；旧资料已保留，下次导入将先恢复",
                    )
                    raise SourceImportError("提交恢复未完成，需要重试恢复") from None
            if published:
                raise SourceImportPublishedError() from None
            cancelled = isinstance(exc, (_Cancelled, asyncio.CancelledError))
            capacity = exc if isinstance(exc, SourceImportCapacityError) else None
            result = service._state(
                directory,
                source_id,
                "cancelled" if cancelled else "failed",
                retryable=True,
                error=capacity.public_message
                if capacity
                else "导入已取消"
                if cancelled
                else "导入失败，旧资料保持不变",
                error_code=capacity.code if capacity else None,
                **self.gc(directory),
            )
            if isinstance(exc, asyncio.CancelledError):
                raise
            if cancelled:
                return result
            if isinstance(exc, SourceImportError):
                raise
            raise SourceImportError("导入失败，旧资料保持不变；请检查服务日志后重试") from None
        finally:
            service._tasks.pop(source_id, None)
            service._cancelled.discard(source_id)
            lock.close()
