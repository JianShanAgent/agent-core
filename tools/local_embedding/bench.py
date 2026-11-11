"""量本机嵌入服务的速度：从知识库切片里均匀抽样，分批发给 /v1/embeddings，估算全量用时。

只用标准库，直接用系统 python 跑（服务要先起来）：

    python3 tools/local_embedding/bench.py --sample 200 --batch 16
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def post(url: str, payload: dict) -> dict:
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=600) as response:
        return json.load(response)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8765/v1/embeddings")
    parser.add_argument("--chunks", type=Path, default=ROOT / "data/kb/chunks.jsonl")
    parser.add_argument("--sample", type=int, default=200)
    parser.add_argument("--batch", type=int, default=16)
    args = parser.parse_args()

    texts = []
    with args.chunks.open(encoding="utf-8") as lines:
        for line in lines:
            chunk = json.loads(line)
            if chunk.get("embed", True):
                texts.append(chunk["text"])
    total_chunks, total_chars = len(texts), sum(map(len, texts))
    step = max(total_chunks // args.sample, 1)
    sample = texts[::step][: args.sample]

    post(args.url, {"input": sample[:2]})  # 预热，不计时
    tokens = 0
    started = time.perf_counter()
    for start in range(0, len(sample), args.batch):
        reply = post(
            args.url, {"input": sample[start : start + args.batch], "encoding_format": "base64"}
        )
        tokens += reply["usage"]["total_tokens"]
    elapsed = time.perf_counter() - started

    chars = sum(map(len, sample))
    eta = elapsed / chars * total_chars
    print(f"抽样 {len(sample)} 条 / {chars} 字 / {tokens} token，用时 {elapsed:.1f}s")
    speed = f"{len(sample) / elapsed:.2f} 条/s，{tokens / elapsed:.0f} token/s"
    print(f"速度 {speed}（批大小 {args.batch}）")
    print(f"全量 {total_chunks} 条 / {total_chars} 字，预计 {eta / 60:.0f} 分钟")


if __name__ == "__main__":
    main()
