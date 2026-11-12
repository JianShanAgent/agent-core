"""以图搜图：拿一张图，在 wiki 图片索引里找最像的几张（注册成服务 ``image_search``）。

索引由离线管线准备好（``images collect`` → ``images embed`` → ``images load``，见 cli.py），
这里只管查：

1. 图片先过 ``core.images.normalize_image``（铺白底、长边缩到 ``max_side``、转成 JPEG）。
   入库的图走的是同一个函数、同样的参数（都从配置的 ``images`` 段读），两边的向量才对得上；
2. 调嵌入接口把它算成向量（和文本检索一样收紧了超时和重试：用户在等回复）；
3. 在库里按余弦相似度找最像的，同一个造型的几张图（立绘大图、卡面小图）只留最像的一张。

返回哪些、哪张算「就是这张图」（数字是实测定的，见 tools/kb_eval/eval_images.py 和配置里的注释）：

- 相似度低于 ``min_score`` 的不返回。
- ``sure``（可以当成就是这张图）只给最像的那一张，见 :func:`is_sure`。光看相似度高不高是不够的：
  画风相近的立绘之间本来就有 0.7 上下的相似度，而截图、海报里的立绘和原图也只有 0.6～0.8。
  真正管用的是「领先幅度」——库里有这张图时，它会明显比第二像的造型更像；
  库里没有时，前几名的相似度是挤在一起的。
- 认准了（``sure``）就只返回这一张；没认准时返回最像的几张，都只算「有些像」。

索引是空的、或者库里的向量和现在的配置对不上（指纹或预处理不一样）时，不调嵌入接口，直接返回空。

部署挂了几个 wiki 时，一个 wiki 一个 wiki 地找、各自判断认没认准，再合起来：同一张立绘两个 wiki
上都有，放在一起比的话它们互为第二名、相似度挤在一起，就永远认不准了。有 wiki 认准了，
就只返回认准的（每个 wiki 至多一张）；都没认准，返回各个 wiki 里最像的几张。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

from jianshanagent.core.embedding import EmbeddingClient
from jianshanagent.core.images import DEFAULT_MAX_SIDE, normalize_image
from jianshanagent.core.logging import get_logger
from jianshanagent.plugins.knowledge.image_store import ImageStore, ImageStoreInfo

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ImageSettings:
    """``plugin.knowledge`` 的 ``images`` 段：找图的门槛，和算向量前的预处理。

    收哪些图由站点插件定，是插件自己的设置。"""

    #: 一次最多返回几张
    top_k: int = 3
    #: 相似度低于它的不返回
    min_score: float = 0.65
    #: 最像的那张不低于它，才可能标成「就是这张图」
    sure_score: float = 0.65
    #: 相似度刚到 sure_score 时，还要比第二像的造型高出这么多；相似度越接近 1，要求的越少
    sure_margin: float = 0.1
    #: 算向量前的预处理：长边缩到多少、JPEG 质量。入库和查询共用，改了要重新 embed 和 load
    max_side: int = DEFAULT_MAX_SIDE
    jpeg_quality: int = 90
    #: 全量算向量时一次请求几张图
    embed_batch_size: int = 8

    @property
    def preprocess(self) -> str:
        """预处理的写法：和向量存在一起，库里的和现在配置的不一样就不能比。"""
        return f"flatten=white;max_side={self.max_side};jpeg={self.jpeg_quality}"

    @classmethod
    def from_config(cls, raw: dict[str, Any]) -> ImageSettings:
        defaults = cls()

        def number(key: str, default: float) -> float:
            return float(raw[key]) if raw.get(key) is not None else default

        return cls(
            top_k=max(1, int(raw.get("top_k") or defaults.top_k)),
            min_score=number("min_score", defaults.min_score),
            sure_score=number("sure_score", defaults.sure_score),
            sure_margin=number("sure_margin", defaults.sure_margin),
            max_side=max(1, int(raw.get("max_side") or defaults.max_side)),
            jpeg_quality=int(raw.get("jpeg_quality") or defaults.jpeg_quality),
            embed_batch_size=max(1, int(raw.get("embed_batch_size") or defaults.embed_batch_size)),
        )


@dataclass(frozen=True, slots=True)
class ImageHit:
    #: 库里的 id：``<wiki>:<wiki 上的文件名>``
    image_id: str
    #: 给人看的一句话
    label: str
    #: 式神立绘 / 皮肤立绘 / ……
    kind: str
    page_title: str
    #: 所属页面的链接
    url: str
    image_url: str
    #: 和查询图片的余弦相似度
    score: float
    #: 可以当成就是这张图（见 :func:`is_sure`）
    sure: bool
    #: 给大模型当资料的说明文字
    text: str
    meta: dict[str, Any]
    #: 出自哪个 wiki（``core.wiki.yaml`` 里的 id）
    wiki: str = ""


def is_sure(score: float, runner_up: float | None, settings: ImageSettings) -> bool:
    """最像的那个造型（相似度 ``score``）能不能当成「就是这张图」。

    ``runner_up`` 是第二像的造型的相似度（库里只有一个造型时为 None）。两个条件：

    - 自己够像：``score`` 不低于 ``sure_score``；
    - 明显比第二名更像：领先幅度不小于 ``sure_margin × (1 - score) / (1 - sure_score)``。
      也就是相似度刚到 ``sure_score`` 时要领先 ``sure_margin``，越接近 1 要求越少：
      原图的一份拷贝（相似度 0.99）只要比它的换色版、臻藏版高一点就行；
      截图里抠不干净的立绘（相似度 0.7）要比别的造型高出一截才算数。
    """
    if score < settings.sure_score:
        return False
    if runner_up is None:
        return True
    needed = settings.sure_margin * (1 - score) / max(1 - settings.sure_score, 1e-9)
    return score - runner_up >= needed


class ImageSearch:
    def __init__(
        self,
        store: ImageStore,
        embedder: EmbeddingClient,
        settings: ImageSettings | None = None,
    ):
        self.store = store
        self.embedder = embedder
        self.settings = settings or ImageSettings()

    async def aclose(self) -> None:
        await self.embedder.aclose()

    def _unusable(self, info: ImageStoreInfo) -> str | None:
        """一个 wiki 的图片向量现在不能拿来比的原因；能比就是 None。"""
        if not info.with_vectors:
            return "图片索引是空的"
        fingerprint = self.embedder.provider.fingerprint
        if info.fingerprint != fingerprint:
            return f"库里图片向量的指纹是「{info.fingerprint}」，现在配置的是「{fingerprint}」"
        if info.preprocess != self.settings.preprocess:
            return (
                f"库里的图片入库时的预处理是「{info.preprocess}」，"
                f"现在配置的是「{self.settings.preprocess}」"
            )
        return None

    async def search(self, image: bytes, *, top_k: int | None = None) -> list[ImageHit]:
        """``image`` 是原始图片字节（任意常见格式）。

        只返回相似度不低于 ``min_score`` 的，按相似度从高到低，最多 ``top_k`` 张；
        最像的那张认准了（``sure``）时只返回它一张。
        图片识别不了抛 ``jianshanagent.core.images.ImageError``，嵌入接口出错抛 ``EmbeddingError``。
        """
        started = time.perf_counter()
        settings = self.settings
        limit = top_k or settings.top_k
        # 解码和缩放大图要几十到上百毫秒，放到线程里做，不卡住别的消息
        jpeg = await asyncio.to_thread(
            normalize_image, image, max_side=settings.max_side, quality=settings.jpeg_quality
        )
        info = await self.store.info()
        usable = [wiki for wiki, mine in info.wikis.items() if self._unusable(mine) is None]
        skipped = None if usable else self._unusable(info)
        hits: list[ImageHit] = []
        if usable:
            vector = await self.embedder.embed_image(jpeg)
            for wiki in usable:
                hits += await self._search_wiki(vector, wiki, limit)
            # 认准了的排前面；有认准的就只给认准的
            hits.sort(key=lambda hit: (not hit.sure, -hit.score, hit.image_id))
            hits = [hit for hit in hits if hit.sure] or hits[:limit]
        # 索引还没建（空的）不算异常；建了却对不上，要让人看见
        log = logger.warning if skipped and info.with_vectors else logger.info
        log(
            "image_search",
            hits=[(hit.image_id, round(hit.score, 3)) for hit in hits],
            sure=bool(hits) and hits[0].sure,
            skipped=skipped,
            seconds=round(time.perf_counter() - started, 2),
        )
        return hits

    async def _search_wiki(self, vector: Any, wiki: str, limit: int) -> list[ImageHit]:
        """在一个 wiki 的图片里找：认准了只给那一张，没认准给最像的几张。"""
        settings = self.settings
        # 至少取两个造型：判断「就是这张图」要看第二名落后多少，
        # 哪怕调用方只要 1 张、哪怕第二名自己不够格返回
        matches = await self.store.search(vector, limit=max(limit, 2), wiki=wiki)
        runner_up = matches[1].score if len(matches) > 1 else None
        sure = bool(matches) and is_sure(matches[0].score, runner_up, settings)
        # 认准了就只给这一张：后面几名明显不如它像，给出去只会让回答里多出「也有点像某某」
        return [
            ImageHit(
                image_id=match.image.image_id,
                label=match.image.label,
                kind=match.image.kind,
                page_title=match.image.page_title,
                url=match.image.url,
                image_url=match.image.image_url,
                score=match.score,
                sure=sure,
                text=match.image.text,
                meta=match.image.meta,
                wiki=match.image.wiki,
            )
            for match in matches[: 1 if sure else limit]
            if match.score >= settings.min_score
        ]
