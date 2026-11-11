# /// script
# requires-python = ">=3.11,<3.13"
# dependencies = [
#     "torch>=2.8",
#     "transformers>=4.57.1,<5",
#     "numpy",
# ]
# ///
"""本机嵌入服务：在 Apple Silicon 上跑 Qwen3-VL-Embedding，提供 OpenAI 兼容的 /v1/embeddings。

用途：知识库首轮全量嵌入在本机做，省掉云端费用；之后的增量和查询走硅基流动的同一个模型。
两边的向量必须可比，所以这里严格按官方脚本（模型仓库里的 scripts/qwen3_vl_embedding.py）的做法：
系统提示放指令、用户消息放正文、带生成提示、取最后一个有效 token 的隐状态、L2 归一化。
只处理文本（知识库切片都是文本），不加载图像和视频的预处理依赖。

运行（在仓库根目录）：

    uv run tools/local_embedding/serve.py --selfcheck          # 只跑官方示例核对，不起服务
    uv run tools/local_embedding/serve.py                      # 起服务，默认 127.0.0.1:8765

接口：

    POST /v1/embeddings  {"input": "文本" | ["文本", ...], "dimensions": 可选,
                          "encoding_format": "float" | "base64",
                          "instruction": 可选，默认 "Represent the user's input."}
    GET  /v1/models      模型名
    GET  /health         设备、精度、维度、指令等，用来生成向量指纹
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import threading
import time
import unicodedata
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import numpy as np
import torch
import torch.nn.functional as F
from transformers import AutoTokenizer
from transformers.models.qwen3_vl.modeling_qwen3_vl import (
    Qwen3VLConfig,
    Qwen3VLModel,
    Qwen3VLPreTrainedModel,
)

DEFAULT_MODEL_DIR = Path(__file__).resolve().parents[2] / "data/models/Qwen3-VL-Embedding-8B"
DEFAULT_INSTRUCTION = "Represent the user's input."
MODEL_ID = "Qwen/Qwen3-VL-Embedding-8B"
MAX_LENGTH = 8192  # 与官方脚本一致

# 模型卡上的示例：4 个查询对第 1 篇文档的相似度（官方脚本的输出）
_REF_QUERIES = [
    "A woman playing with her dog on a beach at sunset.",
    "Pet owner training dog outdoors near water.",
    "Woman surfing on waves during a sunny day.",
    "City skyline view from a high-rise building at night.",
]
_REF_DOCUMENT = (
    "A woman shares a joyful moment with her golden retriever on a sun-drenched beach at sunset, "
    "as the dog offers its paw in a heartwarming display of companionship and trust."
)
_REF_SCORES = [0.74267578125, 0.443603515625, 0.3671875, 0.060821533203125]


class Qwen3VLForEmbedding(Qwen3VLPreTrainedModel):
    """与官方脚本同名同结构的外壳：权重的键以 ``model.`` 开头。"""

    _checkpoint_conversion_mapping: dict[str, str] = {}
    accepts_loss_kwargs = False
    config_class = Qwen3VLConfig

    def __init__(self, config: Qwen3VLConfig):
        super().__init__(config)
        self.model = Qwen3VLModel(config)
        self.post_init()

    def forward(self, input_ids: torch.Tensor, attention_mask: torch.Tensor) -> torch.Tensor:
        return self.model(input_ids=input_ids, attention_mask=attention_mask).last_hidden_state


class Embedder:
    def __init__(self, model_dir: Path, *, device: str, dtype: str, max_batch_tokens: int):
        self.model_dir = model_dir
        self.device = torch.device(device)
        self.dtype_name = dtype
        self.max_batch_tokens = max_batch_tokens
        self.tokenizer = AutoTokenizer.from_pretrained(model_dir, padding_side="right")
        started = time.perf_counter()
        self.model = Qwen3VLForEmbedding.from_pretrained(model_dir, dtype=getattr(torch, dtype))
        self.model.to(self.device).eval()
        self.dim = int(self.model.config.text_config.hidden_size)
        self.load_seconds = time.perf_counter() - started
        self._lock = threading.Lock()  # 模型一次只算一批，请求排队

    def _prompt(self, text: str, instruction: str) -> str:
        instruction = instruction.strip()
        if instruction and not unicodedata.category(instruction[-1]).startswith("P"):
            instruction += "."  # 官方脚本：指令末尾补标点
        conversation = [
            {"role": "system", "content": [{"type": "text", "text": instruction}]},
            {"role": "user", "content": [{"type": "text", "text": text or "NULL"}]},
        ]
        return self.tokenizer.apply_chat_template(
            conversation, add_generation_prompt=True, tokenize=False
        )

    @torch.inference_mode()
    def _encode_batch(self, prompts: list[str]) -> tuple[np.ndarray, int]:
        batch = self.tokenizer(
            prompts, padding=True, truncation=True, max_length=MAX_LENGTH, return_tensors="pt"
        ).to(self.device)
        hidden = self.model(batch["input_ids"], batch["attention_mask"])
        last = batch["attention_mask"].sum(dim=1) - 1  # 右填充：最后一个有效 token
        pooled = hidden[torch.arange(hidden.shape[0], device=hidden.device), last]
        pooled = F.normalize(pooled.float(), p=2, dim=-1)
        return pooled.cpu().numpy(), int(batch["attention_mask"].sum())

    def embed(
        self, texts: list[str], *, instruction: str = DEFAULT_INSTRUCTION
    ) -> tuple[np.ndarray, int]:
        """返回（按输入顺序的 float32 向量，token 总数）。内部按长度排序分批，减少填充。"""
        prompts = [self._prompt(text, instruction) for text in texts]
        lengths = [
            len(ids)
            for ids in self.tokenizer(prompts, truncation=True, max_length=MAX_LENGTH)["input_ids"]
        ]
        order = sorted(range(len(prompts)), key=lengths.__getitem__)
        out = np.zeros((len(prompts), self.dim), dtype=np.float32)
        total_tokens = 0
        with self._lock:
            start = 0
            while start < len(order):
                end = start + 1  # 一批的开销按「条数 × 最长那条」算，不超过上限
                while (
                    end < len(order)
                    and (end - start + 1) * lengths[order[end]] <= self.max_batch_tokens
                ):
                    end += 1
                index = order[start:end]
                vectors, tokens = self._encode_batch([prompts[i] for i in index])
                out[index] = vectors
                total_tokens += tokens
                start = end
            if self.device.type == "mps":
                torch.mps.empty_cache()
        return out, total_tokens

    def info(self) -> dict[str, Any]:
        return {
            "model": MODEL_ID,
            "provider": "local",
            "device": self.device.type,
            "dtype": self.dtype_name,
            "dimensions": self.dim,
            "max_length": MAX_LENGTH,
            "default_instruction": DEFAULT_INSTRUCTION,
            "pooling": "last_token",
            "normalized": True,
            "torch": torch.__version__,
            "load_seconds": round(self.load_seconds, 1),
        }


def selfcheck(embedder: Embedder) -> bool:
    vectors, _ = embedder.embed([*_REF_QUERIES, _REF_DOCUMENT])
    scores = (vectors[:4] @ vectors[4]).tolist()
    worst = max(abs(a - b) for a, b in zip(scores, _REF_SCORES, strict=True))
    print("官方示例相似度：", [round(s, 4) for s in _REF_SCORES])
    print("本机算出的    ：", [round(s, 4) for s in scores])
    print(f"最大偏差 {worst:.4f}（阈值 0.01）")
    return worst <= 0.01


def make_handler(embedder: Embedder) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _send(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _error(self, status: int, message: str) -> None:
            self._send(status, {"error": {"message": message, "type": "invalid_request_error"}})

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/health":
                self._send(200, {"ok": True, **embedder.info()})
            elif self.path == "/v1/models":
                self._send(200, {"object": "list", "data": [{"id": MODEL_ID, "object": "model"}]})
            else:
                self._error(404, "not found")

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/v1/embeddings":
                return self._error(404, "not found")
            try:
                request = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
                raw = request["input"]
                texts = [raw] if isinstance(raw, str) else list(raw)
                if not texts or not all(isinstance(t, str) for t in texts):
                    raise ValueError("input 必须是字符串或字符串数组")
                dimensions = int(request.get("dimensions") or embedder.dim)
                if not 64 <= dimensions <= embedder.dim:
                    raise ValueError(f"dimensions 必须在 64 到 {embedder.dim} 之间")
                instruction = str(request.get("instruction") or DEFAULT_INSTRUCTION)
                as_base64 = request.get("encoding_format") == "base64"
            except (KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
                return self._error(400, str(exc))

            started = time.perf_counter()
            vectors, tokens = embedder.embed(texts, instruction=instruction)
            if dimensions < embedder.dim:  # 套娃表示：截断后重新归一化
                vectors = vectors[:, :dimensions]
                vectors = vectors / np.linalg.norm(vectors, axis=1, keepdims=True)
            data = [
                {
                    "object": "embedding",
                    "index": i,
                    "embedding": base64.b64encode(v.astype("<f4").tobytes()).decode()
                    if as_base64
                    else v.tolist(),
                }
                for i, v in enumerate(vectors)
            ]
            self._send(
                200,
                {
                    "object": "list",
                    "model": MODEL_ID,
                    "data": data,
                    "usage": {"prompt_tokens": tokens, "total_tokens": tokens},
                },
            )
            elapsed = time.perf_counter() - started
            print(
                f"embed n={len(texts)} tokens={tokens} {elapsed:.2f}s {tokens / elapsed:.0f} tok/s",
                flush=True,
            )

        def log_message(self, *_args: Any) -> None:  # 默认的访问日志太吵，上面自己打
            return

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser(description="本机 Qwen3-VL-Embedding 嵌入服务")
    parser.add_argument("--model-dir", type=Path, default=DEFAULT_MODEL_DIR)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--device", default="mps" if torch.backends.mps.is_available() else "cpu")
    parser.add_argument("--dtype", default="float16", choices=["float16", "bfloat16", "float32"])
    parser.add_argument(
        "--max-batch-tokens", type=int, default=16384, help="一批的上限：条数 × 最长那条的 token 数"
    )
    parser.add_argument("--selfcheck", action="store_true", help="只跑官方示例核对，不起服务")
    args = parser.parse_args()

    print(f"加载 {args.model_dir}（{args.device}, {args.dtype}）…", flush=True)
    embedder = Embedder(
        args.model_dir, device=args.device, dtype=args.dtype, max_batch_tokens=args.max_batch_tokens
    )
    print(f"加载完成，用时 {embedder.load_seconds:.1f}s，维度 {embedder.dim}", flush=True)

    ok = selfcheck(embedder)
    if args.selfcheck:
        return 0 if ok else 1
    if not ok:
        print("与官方示例偏差过大，不启动服务。", file=sys.stderr)
        return 1

    server = ThreadingHTTPServer((args.host, args.port), make_handler(embedder))
    print(f"服务已启动：http://{args.host}:{args.port}/v1/embeddings", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
