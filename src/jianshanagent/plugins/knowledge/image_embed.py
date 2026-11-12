"""图片索引的后两步：给原图算向量（存成向量文件），再把记录和向量导入数据库。

算向量：读 ``images.jsonl`` → 从缓存里取原图 → ``normalize_image``（和查询时是同一个函数、
同样的参数，都来自配置的 ``images`` 段）→ 调嵌入接口 → 存进 ``<向量目录>/<提供方>-images/``。
向量文件的格式和续跑流程跟文本切片共用（embed.py）：索引里的 ``chunk_id`` 记图片 ID，
``text_hash`` 记「原图哈希:预处理」。所以 wiki 上换了图、或者改了预处理参数，对应的向量会重算；
记录里的文字（名字、说明）变了不用重算。

导入：``images.jsonl`` + 向量文件 → ``kb_images``，整库对齐，可以反复跑。
这一步只读文件、写数据库，不访问 wiki、不调嵌入接口，服务器容器里就能跑。
"""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Awaitable, Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from jianshanagent.core.embedding import (
    EmbeddingClient,
    EmbeddingError,
    EmbeddingProvider,
    EmbeddingResult,
)
from jianshanagent.core.images import ImageError, normalize_image
from jianshanagent.plugins.knowledge.embed import (
    INDEX_FILE,
    EmbedReport,
    VectorStore,
    VerifyResult,
    embed_items,
    status_of,
    verify_items,
)
from jianshanagent.plugins.knowledge.image_collect import original_path, read_images
from jianshanagent.plugins.knowledge.image_store import (
    ImageRecord,
    ImageStore,
    ImageSyncReport,
    KbImage,
)
from jianshanagent.plugins.knowledge.images import ImageSettings

#: 写进向量目录 ``meta.json`` 的说明：这里面是图片的向量
_EMBEDDED = "image"


def vectors_dirname(provider: EmbeddingProvider) -> str:
    """图片向量的目录名：和这一家的文本向量（``<提供方>/``）分开放。"""
    return f"{provider.name}-images"


def embed_key(image: KbImage, preprocess: str) -> str:
    """向量对不对得上的依据：原图内容 + 算向量前的预处理，哪一样变了都要重算。"""
    return f"{image.sha1}:{preprocess}"


@dataclass(frozen=True, slots=True)
class PendingImage:
    #: 图片 ID（向量文件的索引里这一栏叫 chunk_id）
    chunk_id: str
    digest: str
    #: 原图在缓存里的位置
    path: Path


def pending_images(
    images: Sequence[KbImage], cache_dir: Path, settings: ImageSettings
) -> list[PendingImage]:
    return [
        PendingImage(
            image.image_id, embed_key(image, settings.preprocess), original_path(cache_dir, image)
        )
        for image in images
    ]


def images_without_vectors(
    provider: EmbeddingProvider,
    images: Sequence[KbImage],
    store_dir: Path,
    settings: ImageSettings,
) -> list[KbImage]:
    """还没有向量的图片（wiki 上换过图、改过预处理参数的也算）。只看向量文件，不发请求。

    自动更新用它决定下载哪些原图：已经有向量的图片不需要原图，所以一台只有记录和向量、
    没有原图缓存的机器（服务器）也能接着更新，不用把一个 G 的原图再下一遍。
    """
    store = VectorStore(store_dir, provider, embedded=_EMBEDDED)
    store.open()
    store.close()
    return [
        image
        for image in images
        if not store.has(image.image_id, embed_key(image, settings.preprocess))
    ]


def image_embedder(
    settings: ImageSettings,
) -> Callable[[EmbeddingClient, list[PendingImage]], Awaitable[EmbeddingResult]]:
    """把一批原图算成向量的做法：读文件、预处理（放到线程里，不卡住别的请求）、调接口。"""

    def prepare(batch: list[PendingImage]) -> list[bytes]:
        return [
            normalize_image(
                item.path.read_bytes(), max_side=settings.max_side, quality=settings.jpeg_quality
            )
            for item in batch
        ]

    async def embed_batch(client: EmbeddingClient, batch: list[PendingImage]) -> EmbeddingResult:
        try:
            jpegs = await asyncio.to_thread(prepare, batch)
        except (ImageError, OSError) as exc:
            names = "、".join(item.chunk_id for item in batch)
            raise EmbeddingError(f"这一批里有原图读不出来（{names}）：{exc}") from exc
        return await client.embed_images(jpegs)

    return embed_batch


@dataclass(slots=True)
class ImageEmbedReport:
    report: EmbedReport
    #: 原图不在缓存里、这次没法算的图片
    no_original: list[str] = field(default_factory=list)


def _with_everything(
    report: EmbedReport,
    provider: EmbeddingProvider,
    everything: list[PendingImage],
    store_dir: Path,
) -> ImageEmbedReport:
    """``report`` 只数了原图在缓存里的；把总数、已有数换成按全部记录算的。"""
    store = VectorStore(store_dir, provider, embedded=_EMBEDDED)
    store.open()
    store.close()
    current = {(item.chunk_id, item.digest) for item in everything}
    report.total = len(current)
    report.stale = len(set(store.rows) - current)
    report.already = len(current & set(store.rows)) - report.embedded
    return ImageEmbedReport(
        report,
        no_original=[
            item.chunk_id
            for item in everything
            if not store.has(item.chunk_id, item.digest) and not item.path.exists()
        ],
    )


async def embed_image_files(
    provider: EmbeddingProvider,
    images_path: Path,
    store_dir: Path,
    cache_dir: Path,
    settings: ImageSettings,
    *,
    limit: int | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    retry_base_seconds: float = 1.0,
) -> ImageEmbedReport:
    """给还没有向量的图片算向量。``limit``：这次最多算多少张（试跑用）。"""
    everything = pending_images(read_images(images_path), cache_dir, settings)
    report = await embed_items(
        provider,
        [item for item in everything if item.path.exists()],
        store_dir,
        image_embedder(settings),
        batch_size=settings.embed_batch_size,
        limit=limit,
        embedded=_EMBEDDED,
        transport=transport,
        retry_base_seconds=retry_base_seconds,
    )
    return _with_everything(report, provider, everything, store_dir)


async def verify_image_vectors(
    provider: EmbeddingProvider,
    images_path: Path,
    store_dir: Path,
    cache_dir: Path,
    settings: ImageSettings,
    *,
    sample: int = 20,
    transport: httpx.AsyncBaseTransport | None = None,
) -> VerifyResult:
    """随机抽几张，每张单独再算一次，和存下来的向量比（相似度应接近 1）。"""
    everything = pending_images(read_images(images_path), cache_dir, settings)
    return await verify_items(
        provider,
        [item for item in everything if item.path.exists()],
        store_dir,
        image_embedder(settings),
        sample=sample,
        embedded=_EMBEDDED,
        transport=transport,
    )


@dataclass(slots=True)
class ImageStatus:
    #: 各类图片的张数
    by_kind: dict[str, int]
    #: 原图不在缓存里的张数
    no_original: int
    vectors: EmbedReport


def image_status(
    provider: EmbeddingProvider,
    images_path: Path,
    store_dir: Path,
    cache_dir: Path,
    settings: ImageSettings,
) -> ImageStatus:
    """只看现状，不发任何请求。"""
    images = read_images(images_path)
    everything = pending_images(images, cache_dir, settings)
    return ImageStatus(
        by_kind=dict(Counter(image.kind for image in images).most_common()),
        no_original=sum(1 for item in everything if not item.path.exists()),
        vectors=status_of(provider, everything, store_dir, embedded=_EMBEDDED),
    )


def read_image_records(
    images_path: Path,
    vectors_dir: Path,
    provider: EmbeddingProvider,
    preprocess: str,
    wiki: str = "",
) -> Iterator[ImageRecord]:
    """逐张读出（图片，向量）。向量按「图片 ID + 原图哈希 + 预处理」对，对不上的当作没有。

    ``wiki``：这份记录是哪个 wiki 的，读出来的图片上标着它（入库的样子）。
    """
    vectors = VectorStore(vectors_dir, provider, embedded=_EMBEDDED)
    has_vectors = (vectors_dir / INDEX_FILE).exists()
    if has_vectors:
        vectors.open()  # 顺带核对指纹；目录里是别的指纹的向量会在这里报错
        vectors.close()
    for image in read_images(images_path):
        position = (
            vectors.row(image.image_id, embed_key(image, preprocess)) if has_vectors else None
        )
        yield image.of(wiki), (vectors.vector(position) if position is not None else None)


async def load_images_into(
    store: ImageStore,
    images_path: Path,
    vectors_dir: Path,
    provider: EmbeddingProvider,
    preprocess: str,
    *,
    wiki: str = "",
    replace_unlabelled: bool = False,
) -> ImageSyncReport:
    """``replace_unlabelled``：库里没标出处的图片一起换掉（导入主 wiki 时用）。"""
    return await store.sync(
        read_image_records(images_path, vectors_dir, provider, preprocess, wiki),
        fingerprint=provider.fingerprint,
        preprocess=preprocess,
        wiki=wiki,
        replace_unlabelled=replace_unlabelled,
    )
