"""嵌入调用层：OpenAI 兼容的 ``/embeddings`` 接口（模力方舟、本机模型服务）。
文字和图片都能算，算出来的向量在同一个空间里。

只依赖 httpx，不引入 numpy：向量就是 4096 个单精度小数，用标准库的 ``array('f')`` 装。

几条实测出来的约束在这里统一处理：

- 每次请求都带 ``dimensions``：模力方舟不带时只返回前 1024 维，而且不归一化。
- 返回的 ``index`` 不一定可信（硅基流动一次超过 8 条时每 8 条从 0 重数）：只有它正好是
  0..n-1 各一个时才按它排，否则信返回顺序；条数对不上直接报错，不猜。
- 向量统一归一化成长度 1（各家默认都已归一化，这里只是兜底），之后点积就是余弦相似度。
- 模力方舟偶发 400（重试即好），所以 400 也重试；真的是参数有问题时重试几次后照样报错。
"""

from __future__ import annotations

import asyncio
import base64
import math
import sys
from array import array
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from jianshanagent.core.config import AppConfig
from jianshanagent.core.images import jpeg_data_uri
from jianshanagent.core.logging import get_logger

logger = get_logger(__name__)

_RETRYABLE_STATUS = {400, 408, 429, 500, 502, 503, 504}
#: 长度偏离 1 超过这个值才重新归一化（半精度算出来的向量本身就有万分之几的误差）
_NORM_TOLERANCE = 1e-3


class EmbeddingError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class EmbeddingProvider:
    name: str
    base_url: str
    model: str
    api_key: str = ""
    dimensions: int = 4096
    batch_size: int = 16
    concurrency: int = 1
    timeout_seconds: float = 120.0
    max_retries: int = 5

    @property
    def fingerprint(self) -> str:
        """向量指纹：提供方 + 型号 + 维度。指纹不同的向量不能直接比。"""
        return f"{self.name}:{self.model}:{self.dimensions}"


@dataclass(frozen=True, slots=True)
class EmbeddingSettings:
    providers: dict[str, EmbeddingProvider]
    default_provider: str

    def provider(self, name: str | None = None) -> EmbeddingProvider:
        key = name or self.default_provider
        try:
            return self.providers[key]
        except KeyError:
            known = "、".join(self.providers)
            raise EmbeddingError(
                f"core.embedding.yaml 里没有提供方「{key}」（有：{known}）"
            ) from None

    @classmethod
    def from_config(cls, config: AppConfig) -> EmbeddingSettings | None:
        """读取 core.embedding.yaml 与密钥；``enabled: false`` 时返回 None。

        密钥没填的提供方照样读进来，用到它时才报错（本机服务本来就不需要密钥）。
        """
        raw = config.get("core.embedding")
        if not raw.get("enabled", False):
            return None
        providers: dict[str, EmbeddingProvider] = {}
        for name, item in (raw.get("providers") or {}).items():
            item = item or {}
            base_url = str(item.get("base_url") or "").rstrip("/")
            model = str(item.get("model") or "")
            if not base_url or not model:
                raise EmbeddingError(
                    f"core.embedding.yaml 的 providers.{name} 缺少 base_url 或 model"
                )
            secret = str(item.get("secret") or "")
            api_key = str((config.secrets.get(secret) or {}).get("api_key") or "").strip()
            if secret and not api_key:
                logger.warning("embedding_key_missing", provider=name, secret=secret)
            retries = item.get("max_retries")
            providers[str(name)] = EmbeddingProvider(
                name=str(name),
                base_url=base_url,
                model=model,
                api_key=api_key,
                dimensions=int(item.get("dimensions") or 4096),
                batch_size=max(1, int(item.get("batch_size") or 16)),
                concurrency=max(1, int(item.get("concurrency") or 1)),
                timeout_seconds=float(item.get("timeout_seconds") or 120),
                max_retries=int(retries) if retries is not None else 5,
            )
        default = str(raw.get("default_provider") or "")
        if default not in providers:
            raise EmbeddingError(
                f"core.embedding.yaml 的 default_provider「{default}」未在 providers 中定义"
            )
        return cls(providers=providers, default_provider=default)


@dataclass(slots=True)
class EmbeddingResult:
    #: 和输入一一对应、已归一化的向量
    vectors: list[array[float]]
    prompt_tokens: int = 0


def vector_bytes(vector: array[float]) -> bytes:
    """向量的存盘格式：单精度、小端，一个接一个。"""
    if sys.byteorder == "little":
        return vector.tobytes()
    swapped = array("f", vector)
    swapped.byteswap()
    return swapped.tobytes()


def vector_from_bytes(raw: bytes) -> array[float]:
    vector = array("f")
    vector.frombytes(raw)
    if sys.byteorder != "little":
        vector.byteswap()
    return vector


def dot(a: Sequence[float], b: Sequence[float]) -> float:
    return math.fsum(x * y for x, y in zip(a, b, strict=True))


def _normalized(vector: array[float]) -> array[float]:
    norm = math.sqrt(math.fsum(x * x for x in vector))
    if norm == 0 or not math.isfinite(norm):
        raise EmbeddingError("接口返回了全零或非法的向量")
    if abs(norm - 1.0) <= _NORM_TOLERANCE:
        return vector
    return array("f", (x / norm for x in vector))


def _in_input_order(data: list[dict[str, Any]]) -> list[dict[str, Any]]:
    indexes = [item.get("index") for item in data]
    if sorted(i for i in indexes if isinstance(i, int)) == list(range(len(data))):
        return sorted(data, key=lambda item: item["index"])
    return data


def _parse_vector(raw: Any, dimensions: int) -> array[float]:
    if isinstance(raw, str):
        vector = vector_from_bytes(base64.b64decode(raw))
    else:
        vector = array("f", raw)
    if len(vector) != dimensions:
        raise EmbeddingError(f"接口返回的向量是 {len(vector)} 维，配置要求 {dimensions} 维")
    return _normalized(vector)


class EmbeddingClient:
    def __init__(
        self,
        provider: EmbeddingProvider,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        retry_base_seconds: float = 1.0,
    ):
        self.provider = provider
        self._retry_base_seconds = retry_base_seconds
        headers = {"Content-Type": "application/json"}
        if provider.api_key:
            headers["Authorization"] = f"Bearer {provider.api_key}"
        self._client = httpx.AsyncClient(
            base_url=provider.base_url,
            timeout=provider.timeout_seconds,
            headers=headers,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        last_error: EmbeddingError | None = None
        for attempt in range(self.provider.max_retries + 1):
            if attempt:
                await asyncio.sleep(self._retry_base_seconds * 2 ** (attempt - 1))
            try:
                resp = await self._client.post("/embeddings", json=body)
            except httpx.HTTPError as exc:
                last_error = EmbeddingError(f"嵌入请求失败：{exc!r}", retryable=True)
                continue
            if resp.status_code == 200:
                data: dict[str, Any] = resp.json()
                return data
            error = EmbeddingError(
                f"嵌入接口返回 HTTP {resp.status_code}：{resp.text[:300]}",
                status=resp.status_code,
                retryable=resp.status_code in _RETRYABLE_STATUS,
            )
            if not error.retryable:
                raise error
            last_error = error
        assert last_error is not None
        raise last_error

    async def _embed(self, inputs: list[Any], what: str) -> EmbeddingResult:
        if not inputs:
            return EmbeddingResult(vectors=[])
        provider = self.provider
        data = await self._post(
            {
                "model": provider.model,
                "input": inputs,
                "dimensions": provider.dimensions,
                "encoding_format": "base64",
            }
        )
        items = data.get("data") or []
        if len(items) != len(inputs):
            raise EmbeddingError(f"发了 {len(inputs)} {what}，接口返回了 {len(items)} 个向量")
        vectors = [
            _parse_vector(item.get("embedding"), provider.dimensions)
            for item in _in_input_order(items)
        ]
        usage = data.get("usage") or {}
        return EmbeddingResult(vectors=vectors, prompt_tokens=int(usage.get("prompt_tokens") or 0))

    async def embed(self, texts: Sequence[str]) -> EmbeddingResult:
        """一次请求算一批文字的向量（分批由调用方决定），按输入顺序返回。"""
        return await self._embed(list(texts), "段文字")

    async def embed_one(self, text: str) -> array[float]:
        return (await self.embed([text])).vectors[0]

    async def embed_images(self, images: Sequence[bytes]) -> EmbeddingResult:
        """一次请求算一批图片的向量，按输入顺序返回。和文字的向量在同一个空间，指纹也一样。

        ``images`` 是已经过 ``core.images.normalize_image`` 的 JPEG 字节：入库和查询都先过那一道，
        这里不再处理。图片是把内容编成 base64 放在请求里发的——模力方舟不会自己去下载
        wiki 的图片地址（实测对方下载报 567）。
        """
        return await self._embed([{"image": jpeg_data_uri(image)} for image in images], "张图片")

    async def embed_image(self, image: bytes) -> array[float]:
        return (await self.embed_images([image])).vectors[0]
