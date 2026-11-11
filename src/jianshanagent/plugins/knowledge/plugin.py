"""knowledge 插件的运行时部分：把知识库检索注册成服务（``knowledge``），供查证工作流取证；
另把以图搜图注册成服务（``image_search``，见 images.py），用户发图问「这是谁」时用。

知识库和图片索引的内容由离线管线准备好（``run`` → ``embed`` → ``load``，图片是
``images collect`` → ``images embed`` → ``images load``，见 cli.py），这里只管查。
需要数据库；没有数据库（本机调试）时不注册，机器人照常运行。
"""

from __future__ import annotations

from typing import Any

from jianshanagent.core.embedding import EmbeddingClient, EmbeddingError, EmbeddingSettings
from jianshanagent.core.logging import get_logger
from jianshanagent.core.plugin import PluginContext
from jianshanagent.plugins.knowledge.image_store import PostgresImageStore
from jianshanagent.plugins.knowledge.images import ImageSearch, ImageSettings
from jianshanagent.plugins.knowledge.retrieval import RetrievalSettings, Retriever, query_provider
from jianshanagent.plugins.knowledge.store import PostgresKnowledgeStore

logger = get_logger(__name__)

TYPE_NAMES = {"entry": "条目", "announcement": "公告", "story": "剧情", "data": "数据"}


def retrieval_settings(raw: dict[str, Any]) -> RetrievalSettings:
    defaults = RetrievalSettings()
    retries = raw.get("embed_retries")
    slots = raw.get("keyword_slots")
    return RetrievalSettings(
        top_k=max(1, int(raw.get("top_k") or defaults.top_k)),
        keyword_slots=max(0, int(slots if slots is not None else defaults.keyword_slots)),
        max_df_ratio=float(raw.get("max_df_ratio") or defaults.max_df_ratio),
        embed_timeout_seconds=float(
            raw.get("embed_timeout_seconds") or defaults.embed_timeout_seconds
        ),
        embed_retries=int(retries) if retries is not None else defaults.embed_retries,
    )


class KnowledgePlugin:
    id = "knowledge"

    def register(self, ctx: PluginContext) -> None:
        cfg = ctx.plugin_config().get("retrieval") or {}
        if not cfg.get("enabled", False):
            logger.info("knowledge_retrieval_disabled")
            return
        db = ctx.services.get("database")
        if db is None:
            logger.warning("knowledge_retrieval_needs_database")
            return

        settings = retrieval_settings(cfg)
        embedder: EmbeddingClient | None = None
        image_embedder: EmbeddingClient | None = None
        try:
            embedding = EmbeddingSettings.from_config(ctx.config)
            if embedding is not None:
                provider = query_provider(embedding.provider(), settings)
                embedder = EmbeddingClient(provider)
                # 两个服务各用各的连接：关机时各关各的
                image_embedder = EmbeddingClient(provider)
        except EmbeddingError as exc:
            # 嵌入配置有问题不该让机器人起不来：退回只用关键词
            logger.warning("knowledge_embedding_unavailable", error=str(exc))
        ctx.register_service("knowledge", Retriever(PostgresKnowledgeStore(db), embedder, settings))
        logger.info(
            "knowledge_retrieval_enabled",
            top_k=settings.top_k,
            fingerprint=embedder.provider.fingerprint if embedder else None,
        )
        if image_embedder is None:
            # 以图搜图只有向量一路，嵌入用不了就没法做
            logger.info("image_search_disabled", reason="嵌入接口不可用")
            return
        images = ImageSettings.from_config(ctx.plugin_config().get("images") or {})
        ctx.register_service(
            "image_search", ImageSearch(PostgresImageStore(db), image_embedder, images)
        )
        logger.info(
            "image_search_enabled",
            top_k=images.top_k,
            min_score=images.min_score,
            sure_score=images.sure_score,
            sure_margin=images.sure_margin,
            preprocess=images.preprocess,
        )


def create_plugin() -> KnowledgePlugin:
    return KnowledgePlugin()
