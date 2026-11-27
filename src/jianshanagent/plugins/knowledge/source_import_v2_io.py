"""Small immutable archive primitives. Existing vectors are opened read-only, never repaired."""

from __future__ import annotations

import gzip
import io
import json
import math
import os
import stat
import sys
from array import array
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path
from typing import Any, BinaryIO, TextIO

from jianshanagent.plugins.knowledge.source_import import (
    MAX_LINE_BYTES,
    SourceImportCapacityError,
    SourceImportError,
    _reject_duplicate_keys,
)

_MAX_LEGACY_BYTES = 64 * 1024 * 1024


def _open_read(path: Path) -> BinaryIO:
    if path.is_symlink() or path.parent.is_symlink():
        raise SourceImportError("分片不能是符号链接")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise SourceImportError("分片必须是普通文件")
    return os.fdopen(fd, "rb")


def _row_bytes(row: dict[str, Any]) -> bytes:
    raw = (json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n").encode()
    if len(raw) > MAX_LINE_BYTES:
        raise SourceImportCapacityError("metadata_bytes")
    return raw


def write_rows(path: Path, rows: Iterable[dict[str, Any]]) -> dict[str, tuple[int, int]]:
    """Each JSONL line is its own bounded gzip member, enabling random read by offset."""
    positions: dict[str, tuple[int, int]] = {}
    with path.open("xb") as file:
        for row in rows:
            key = str(row["id"])
            if key in positions:
                raise SourceImportError("原文分片记录身份重复")
            compressed = gzip.compress(_row_bytes(row), mtime=0)
            positions[key] = file.tell(), len(compressed)
            file.write(compressed)
        file.flush()
        os.fsync(file.fileno())
    return positions


def read_row(path: Path, offset: int, length: int) -> dict[str, Any]:
    if (
        type(offset) is not int
        or type(length) is not int
        or offset < 0
        or not 0 < length <= MAX_LINE_BYTES + 1024
    ):
        raise SourceImportError("原文分片引用无效")
    with _open_read(path) as file:
        file.seek(offset)
        compressed = file.read(length)
    if len(compressed) != length:
        raise SourceImportError("原文分片不完整")
    with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as archive:
        raw = archive.read(MAX_LINE_BYTES + 1)
    if len(raw) > MAX_LINE_BYTES:
        raise SourceImportCapacityError("metadata_bytes")
    row = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    if not isinstance(row, dict):
        raise SourceImportError("原文分片必须包含对象")
    return row


def iter_rows(path: Path) -> Iterator[dict[str, Any]]:
    with _open_read(path) as file, gzip.GzipFile(fileobj=file) as archive:
        while line := archive.readline(MAX_LINE_BYTES + 1):
            if len(line) > MAX_LINE_BYTES:
                raise SourceImportCapacityError("metadata_bytes")
            row = json.loads(line, object_pairs_hook=_reject_duplicate_keys)
            if not isinstance(row, dict):
                raise SourceImportError("原文分片必须包含对象")
            yield row


def write_vector_shard(
    path: Path, vectors: Iterable[tuple[str, Sequence[float]]], dimensions: int
) -> dict[str, tuple[int, int]]:
    if type(dimensions) is not int or not 0 < dimensions <= 65536:
        raise SourceImportError("向量维度无效")
    positions: dict[str, tuple[int, int]] = {}
    with path.open("xb") as file:
        for key, values in vectors:
            if key in positions or len(values) != dimensions:
                raise SourceImportError("向量身份重复或维度无效")
            vector = array("f", values)
            if not all(math.isfinite(value) for value in vector):
                raise SourceImportError("向量数值无效")
            if sys.byteorder != "little":
                vector.byteswap()
            raw = vector.tobytes()
            positions[key] = file.tell(), len(raw)
            file.write(raw)
        file.flush()
        os.fsync(file.fileno())
    return positions


def read_vector(path: Path, offset: int, length: int, dimensions: int) -> array[float]:
    if (
        type(dimensions) is not int
        or not 0 < dimensions <= 65536
        or type(offset) is not int
        or offset < 0
        or type(length) is not int
        or length != dimensions * 4
        or offset % 4
    ):
        raise SourceImportError("向量分片引用无效")
    with _open_read(path) as file:
        file.seek(offset)
        raw = file.read(length)
    if len(raw) != length:
        raise SourceImportError("向量分片不完整")
    vector = array("f")
    vector.frombytes(raw)
    if sys.byteorder != "little":
        vector.byteswap()
    if not all(math.isfinite(value) for value in vector):
        raise SourceImportError("向量数值无效")
    return vector


class _JsonStream:
    """A token-sized incremental decoder; never retains a whole array or snapshot."""

    def __init__(self, file: TextIO):
        self.file = file
        self.buffer = ""
        self.position = 0
        self.eof = False
        self.decoder = json.JSONDecoder(object_pairs_hook=_reject_duplicate_keys)

    def more(self) -> bool:
        self.buffer = self.buffer[self.position :]
        self.position = 0
        part = self.file.read(8192)
        if not part:
            self.eof = True
            return False
        self.buffer += part
        return True

    def peek(self) -> str:
        while True:
            while self.position < len(self.buffer) and self.buffer[self.position].isspace():
                self.position += 1
            if self.position < len(self.buffer):
                return self.buffer[self.position]
            if self.eof or not self.more():
                return ""

    def take(self, token: str) -> None:
        if self.peek() != token:
            raise SourceImportError("V1 快照结构损坏")
        self.position += 1

    def value(self) -> Any:
        self.peek()
        while True:
            try:
                value, end = self.decoder.raw_decode(self.buffer, self.position)
                # A number at a buffer boundary may still continue in the next read.
                if end == len(self.buffer) and not self.eof and isinstance(value, (int, float)):
                    self.more()
                    continue
                if len(self.buffer[self.position : end].encode()) > MAX_LINE_BYTES:
                    raise SourceImportCapacityError("metadata_read_memory")
                self.position = end
                return value
            except json.JSONDecodeError:
                if len(self.buffer[self.position :].encode()) > MAX_LINE_BYTES:
                    raise SourceImportCapacityError("metadata_read_memory") from None
                if self.eof or not self.more():
                    raise SourceImportError("V1 快照不是完整合法 JSON") from None


def stream_v1_snapshot(path: Path) -> Iterator[tuple[str, dict[str, Any]]]:
    """Stream a legacy snapshot's rows/records individually, including its fingerprint."""
    with _open_read(path) as binary:
        if os.fstat(binary.fileno()).st_size > _MAX_LEGACY_BYTES:
            raise SourceImportCapacityError("snapshot_read_bytes")
        with io.TextIOWrapper(binary, encoding="utf-8") as text:
            source = _JsonStream(text)
            source.take("{")
            first = True
            keys: set[str] = set()
            while source.peek() != "}":
                if not first:
                    source.take(",")
                first = False
                key = source.value()
                if not isinstance(key, str) or key in keys or len(keys) >= 32:
                    raise SourceImportError("V1 快照顶层字段无效或重复")
                keys.add(key)
                source.take(":")
                if key in {"rows", "records"}:
                    source.take("[")
                    first_item = True
                    while source.peek() != "]":
                        if not first_item:
                            source.take(",")
                        first_item = False
                        value = source.value()
                        if not isinstance(value, dict):
                            raise SourceImportError("V1 快照记录必须是对象")
                        yield "row" if key == "rows" else "record", value
                    source.take("]")
                else:
                    value = source.value()
                    if key == "fingerprint":
                        yield "meta", {"fingerprint": value}
            source.take("}")
            if source.peek():
                raise SourceImportError("V1 快照尾部存在多余内容")
