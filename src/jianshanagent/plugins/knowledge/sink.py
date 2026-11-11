"""输出层：切片和文档清单写到哪里。

管线只认 :class:`ChunkSink` 这个接口；现在的实现是写 JSONL 文件，
以后改成写 Postgres 时再加一个实现同样接口的类即可，管线不用动。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import IO, Any, Protocol

from jianshanagent.plugins.knowledge.models import Chunk

CHUNKS_FILE = "chunks.jsonl"
DOCUMENTS_FILE = "documents.jsonl"


class ChunkSink(Protocol):
    """一次运行的输出。``begin`` 之后写入，``commit`` 才算生效；中途失败调 ``abort`` 丢弃。"""

    def begin(self) -> None: ...

    def write_document(self, record: dict[str, Any]) -> None:
        """一篇来源文档的记录：标识、修订号、状态、切片数（供增量更新时比对）。"""

    def write_chunk(self, chunk: Chunk) -> None: ...

    def commit(self) -> None: ...

    def abort(self) -> None: ...


class JsonlSink:
    """写 ``chunks.jsonl`` 和 ``documents.jsonl``。先写临时文件，提交时原子替换，
    中断的运行不会留下半份输出，也不会破坏上一次的结果。"""

    def __init__(self, output_dir: Path):
        self.output_dir = output_dir
        self._files: dict[str, IO[str]] = {}

    def _tmp(self, name: str) -> Path:
        return self.output_dir / f"{name}.tmp"

    def begin(self) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        for name in (CHUNKS_FILE, DOCUMENTS_FILE):
            self._files[name] = self._tmp(name).open("w", encoding="utf-8")

    def _write(self, name: str, record: dict[str, Any]) -> None:
        self._files[name].write(json.dumps(record, ensure_ascii=False) + "\n")

    def write_document(self, record: dict[str, Any]) -> None:
        self._write(DOCUMENTS_FILE, record)

    def write_chunk(self, chunk: Chunk) -> None:
        self._write(CHUNKS_FILE, chunk.to_json())

    def commit(self) -> None:
        for name, fh in self._files.items():
            fh.close()
            os.replace(self._tmp(name), self.output_dir / name)
        self._files.clear()

    def abort(self) -> None:
        for name, fh in self._files.items():
            fh.close()
            self._tmp(name).unlink(missing_ok=True)
        self._files.clear()
