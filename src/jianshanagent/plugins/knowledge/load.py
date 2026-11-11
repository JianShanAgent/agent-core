"""把切片文件和向量文件导入数据库：``chunks.jsonl`` + ``vectors/<提供方>/`` → ``kb_chunks``。

整库对齐：以切片文件为准，库里多出来的删掉，内容没变的不动（所以可以反复跑）。
还没有向量的切片照样入库，只是只能被关键词查到；补完向量再跑一次就带上了。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

from jianshanagent.core.embedding import EmbeddingProvider
from jianshanagent.plugins.knowledge.embed import VectorStore, text_hash
from jianshanagent.plugins.knowledge.store import KbChunk, KnowledgeStore, Record, SyncReport


def read_records(
    chunks_path: Path, vectors_dir: Path, provider: EmbeddingProvider
) -> Iterator[Record]:
    """逐片读出（切片，向量）。

    向量按「切片编号 + 被算文字的哈希」对，文字变了的旧向量不会被用上。
    """
    vectors = VectorStore(vectors_dir, provider)
    has_vectors = (vectors_dir / "index.jsonl").exists()
    if has_vectors:
        vectors.open()  # 顺带核对指纹；目录里是别的指纹的向量会在这里报错
        vectors.close()
    with chunks_path.open(encoding="utf-8") as lines:
        for line in lines:
            row = json.loads(line)
            chunk = KbChunk.from_json(row)
            position = (
                vectors.row(chunk.chunk_id, text_hash(chunk.text))
                if has_vectors and row.get("embed", True)
                else None
            )
            yield chunk, (vectors.vector(position) if position is not None else None)


async def load_into(
    store: KnowledgeStore, chunks_path: Path, vectors_dir: Path, provider: EmbeddingProvider
) -> SyncReport:
    return await store.sync(
        read_records(chunks_path, vectors_dir, provider), fingerprint=provider.fingerprint
    )
