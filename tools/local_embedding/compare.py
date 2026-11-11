"""对比本机嵌入服务和硅基流动算出的向量是否可以混用。

全量嵌入在本机做、查询走硅基流动，所以要确认两边对同一段文字给出的向量几乎相同，
并且「查询用硅基流动、文档用本机」排出来的名次和「全用硅基流动」一致。

用机器人的环境跑（要读 config/secrets.yaml 里的 siliconflow.api_key；本机服务要先起来）：

    .venv/bin/python tools/local_embedding/compare.py --sample 90
"""

from __future__ import annotations

import argparse
import base64
import json
import statistics
import struct
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
MODEL = "Qwen/Qwen3-VL-Embedding-8B"
QUERIES = [
    "山风的技能是什么",
    "不知火怎么获得",
    "破势御魂的效果",
    "为崽而战最后谁赢了",
    "晴明的皮肤金枫尽染怎么拿",
    "百鬼棋局里觉是几费的棋子",
    "超鬼王孔雀明王的随从速度",
    "狩日之决有哪些章节",
    "茨木童子的传记讲了什么",
    "最近一次维护更新了什么内容",
    "sp 式神和 ssr 有什么区别",
    "御魂 招财猫 效果",
]

Vector = list[float]


def embed(url: str, texts: list[str], *, key: str | None, batch: int) -> list[Vector]:
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    vectors: list[Vector] = []
    for start in range(0, len(texts), batch):
        payload = {
            "model": MODEL,
            "input": texts[start : start + batch],
            "encoding_format": "base64",
        }
        request = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers)
        with urllib.request.urlopen(request, timeout=600) as response:
            # 不按 index 排：硅基流动一次超过 8 条时 index 每 8 条从 0 重新数，返回顺序才是对的
            data = json.load(response)["data"]
        for item in data:
            raw = item["embedding"]
            if isinstance(raw, str):
                blob = base64.b64decode(raw)
                raw = list(struct.unpack(f"<{len(blob) // 4}f", blob))
            vectors.append(raw)
    return vectors


def cosine(a: Vector, b: Vector) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    return dot / (sum(x * x for x in a) ** 0.5 * sum(y * y for y in b) ** 0.5)


def top(query: Vector, docs: list[Vector], k: int) -> list[int]:
    scores = [cosine(query, doc) for doc in docs]
    return sorted(range(len(docs)), key=lambda i: -scores[i])[:k]


def summary(values: list[float]) -> str:
    ordered = sorted(values)
    return (
        f"最低 {ordered[0]:.5f}，5% 分位 {ordered[len(ordered) // 20]:.5f}，"
        f"中位 {statistics.median(ordered):.5f}，平均 {statistics.mean(ordered):.5f}"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--local", default="http://127.0.0.1:8765/v1/embeddings")
    parser.add_argument("--remote", default="https://api.siliconflow.cn/v1/embeddings")
    parser.add_argument("--chunks", type=Path, default=ROOT / "data/kb/chunks.jsonl")
    parser.add_argument("--sample", type=int, default=90)
    parser.add_argument("--batch", type=int, default=8)
    args = parser.parse_args()

    secrets = yaml.safe_load((ROOT / "config/secrets.yaml").read_text(encoding="utf-8"))
    key = (secrets.get("siliconflow") or {}).get("api_key")
    if not key:
        raise SystemExit("config/secrets.yaml 里没有填 siliconflow.api_key")

    with args.chunks.open(encoding="utf-8") as lines:
        chunks = [json.loads(line) for line in lines]
    chunks.sort(key=lambda chunk: chunk["chars"])  # 按长度排好再等距抽，长短都覆盖到
    step = max(len(chunks) // args.sample, 1)
    docs = chunks[step // 2 :: step][: args.sample]
    texts = [chunk["text"] for chunk in docs] + QUERIES

    local = embed(args.local, texts, key=None, batch=args.batch)
    remote = embed(args.remote, texts, key=key, batch=args.batch)
    print(f"维度：本机 {len(local[0])}，硅基流动 {len(remote[0])}")

    same = [cosine(a, b) for a, b in zip(local, remote, strict=True)]
    n = len(docs)
    print(f"同一段文字两边向量的余弦（{len(texts)} 段）：{summary(same)}")
    third = n // 3
    for name, part in (
        ("短", slice(0, third)),
        ("中", slice(third, 2 * third)),
        ("长", slice(2 * third, n)),
    ):
        chars = [chunk["chars"] for chunk in docs[part]]
        print(f"  {name}切片（{min(chars)}–{max(chars)} 字）：{summary(same[part])}")
    print(f"  查询（{len(QUERIES)} 条）：{summary(same[n:])}")
    worst = min(range(len(texts)), key=same.__getitem__)
    print(f"  最低的一段：{same[worst]:.5f}，{len(texts[worst])} 字，开头「{texts[worst][:30]!r}」")

    local_docs, remote_docs = local[:n], remote[:n]
    top1 = overlap = 0
    drift: list[float] = []
    for query_local, query_remote in zip(local[n:], remote[n:], strict=True):
        mixed = top(query_remote, local_docs, 10)  # 上线后的用法：查询走硅基流动，文档是本机算的
        pure = top(query_remote, remote_docs, 10)
        top1 += mixed[0] == pure[0]
        overlap += len(set(mixed) & set(pure))
        drift += [
            abs(cosine(query_remote, local_docs[i]) - cosine(query_remote, remote_docs[i]))
            for i in range(n)
        ]
        del query_local
    count = len(QUERIES)
    print(
        f"混用与全用硅基流动相比：第 1 名相同 {top1}/{count}，前 10 名重合 {overlap}/{count * 10}"
    )
    print(
        f"同一对（查询，文档）的相似度差：最大 {max(drift):.5f}，平均 {statistics.mean(drift):.5f}"
    )


if __name__ == "__main__":
    main()
