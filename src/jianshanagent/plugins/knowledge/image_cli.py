"""``images`` 子命令：wiki 图片索引（以图搜图用）的离线管线。

::

    # 选图、生成记录（data/kb/<wiki 的 id>/images.jsonl）、把原图下载到缓存（只下新增和变了的）
    python -m jianshanagent.plugins.knowledge images collect
    python -m jianshanagent.plugins.knowledge images collect --offline   # 不联网，用缓存重做

    # 给图片算向量（中断后重跑会接着算，已经有的不重算；先试跑可以加 --limit 20）
    python -m jianshanagent.plugins.knowledge images embed

    # 把记录和向量导入数据库（一个 wiki 一个 wiki 地对齐，没变的不动；
    # 只读文件、写数据库，服务器容器里就能跑）
    python -m jianshanagent.plugins.knowledge images load

    # 只看现状（记录、原图缓存、向量、数据库），不发请求
    python -m jianshanagent.plugins.knowledge images status

部署挂了几个 wiki 时，不指明就挨个做；只做一个的话在 ``images`` 前面加 ``--wiki <id>``
（见 cli.py）。没有站点插件管图库的 wiki 跳过。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

from jianshanagent.core.config import AppConfig
from jianshanagent.core.db import Database
from jianshanagent.core.embedding import EmbeddingError, EmbeddingProvider, EmbeddingSettings
from jianshanagent.plugins.knowledge.embed import VectorStoreError
from jianshanagent.plugins.knowledge.fetcher import DiskCache, FetchError, WikiFetcher
from jianshanagent.plugins.knowledge.image_collect import (
    IMAGES_FILE,
    collect_catalog,
    download_originals,
    write_images,
)
from jianshanagent.plugins.knowledge.image_embed import (
    embed_image_files,
    image_status,
    load_images_into,
    vectors_dirname,
    verify_image_vectors,
)
from jianshanagent.plugins.knowledge.image_store import ImageStoreInfo, PostgresImageStore
from jianshanagent.plugins.knowledge.images import ImageSettings
from jianshanagent.plugins.knowledge.pipeline import append_fetch_log
from jianshanagent.plugins.knowledge.settings import Settings, is_primary
from jianshanagent.plugins.knowledge.sources import WikiSource

STATS_FILE = "images_stats.json"
#: 单独重算的向量和存下来的相似度低于这个值，就当作有问题
VERIFY_MIN_SIMILARITY = 0.99


def add_parser(sub: Any) -> None:
    images = sub.add_parser("images", help="wiki 图片索引（以图搜图用）：选图、算向量、导入数据库")
    steps = images.add_subparsers(dest="images_command", required=True)

    collect = steps.add_parser("collect", help="选图、生成记录、下载原图（只下新增和变了的）")
    collect.add_argument("--offline", action="store_true", help="不发任何请求，用缓存重新生成记录")
    collect.add_argument("--output-dir", help="输出目录（默认是知识库目录下这个 wiki 的那一层）")

    embed = steps.add_parser("embed", help="给图片算向量（可断点续跑）")
    embed.add_argument("--limit", type=int, metavar="N", help="这次最多算 N 张（试跑用）")
    embed.add_argument(
        "--verify",
        type=int,
        default=20,
        metavar="N",
        help="算完后随机抽 N 张单独重算，和存下来的比对（默认 20；0 = 不比对）",
    )
    load = steps.add_parser("load", help="把图片记录和向量导入数据库（可反复跑）")
    status = steps.add_parser("status", help="只看现状：记录、原图缓存、向量、数据库")
    for parser in (embed, load, status):
        parser.add_argument(
            "--provider", help="用哪一家（默认取 core.embedding.yaml 的 default_provider）"
        )
        parser.add_argument(
            "--output-dir", help="记录和向量所在的目录（默认是知识库目录下这个 wiki 的那一层）"
        )


def _say(text: str) -> None:
    print(text, file=sys.stderr)


def _output_dir(args: argparse.Namespace, settings: Settings) -> Path:
    return Path(args.output_dir).resolve() if args.output_dir else settings.output_dir


def _provider_and_paths(
    args: argparse.Namespace, config: AppConfig, settings: Settings
) -> tuple[EmbeddingProvider, Path, Path] | None:
    """（提供方，记录文件，这一家的图片向量目录）；配置不对或没有记录文件时说明原因并返回 None。"""
    embedding = EmbeddingSettings.from_config(config)
    if embedding is None:
        _say("core.embedding.yaml 没有启用（enabled: false）")
        return None
    try:
        provider = embedding.provider(args.provider)
    except EmbeddingError as exc:
        _say(str(exc))
        return None
    output_dir = _output_dir(args, settings)
    images_path = output_dir / IMAGES_FILE
    if not images_path.exists():
        if settings.sites.has_images:
            _say(f"找不到图片记录 {images_path}，先跑 images collect")
        else:
            _say(f"「{settings.wiki.id}」没有站点插件管图库，没有图片记录")
        return None
    return provider, images_path, output_dir / "vectors" / vectors_dirname(provider)


# ── collect ───────────────────────────────────────────────────────────


async def _collect(args: argparse.Namespace, settings: Settings) -> int:
    started = time.time()
    output_dir = _output_dir(args, settings)
    if not settings.sites.has_images:
        _say(
            f"「{settings.wiki.id}」没有站点插件管图库：选哪些图、每张图是什么因站而异，"
            "要由站点插件来定（这个 wiki 用哪个站点插件见 core.wiki.yaml；"
            "怎么写见 site.py 的 image_catalog）"
        )
        return 0
    fetcher = None if args.offline else WikiFetcher(settings.wiki, settings.fetch)
    try:
        source = WikiSource(fetcher, DiskCache(settings.cache_dir), settings)
        catalog = await collect_catalog(source, settings)
    except FetchError as exc:
        _say(f"取文件清单和数据失败：{exc}")
        return 2
    finally:
        if fetcher is not None:
            await fetcher.aclose()
    assert catalog is not None
    write_images(output_dir / IMAGES_FILE, catalog.images)
    stats = catalog.stats
    summary = catalog.summary()
    kinds = "、".join(f"{kind} {count}" for kind, count in summary["by_kind"].items())
    _say(
        f"选出 {summary['images']} 张图（{kinds}），"
        f"画的是 {summary['subjects']} 个不同的造型或东西。记录在 {output_dir / IMAGES_FILE}"
    )
    for note in catalog.notes:
        _say(note)
    if catalog.problem:
        _say(f"注意：{catalog.problem}，这份记录多半不全")
    ok = True
    if fetcher is not None:
        download = await download_originals(
            catalog.images, settings.cache_dir, settings.wiki, settings.fetch
        )
        stats["download"] = {
            "total": download.total,
            "cached": download.cached,
            "downloaded": download.downloaded,
            "megabytes": round(download.bytes_received / 1e6, 1),
            "requests": download.requests,
            "failed": download.failed,
            "errors": download.errors[:5],
            "seconds": round(download.seconds, 1),
        }
        _say(
            f"原图：一共要 {download.total} 张，缓存里已有 {download.cached} 张，"
            f"这次下载 {download.downloaded} 张（{download.bytes_received / 1e6:.0f} MB，"
            f"{download.seconds / 60:.1f} 分钟），失败 {len(download.failed)} 张"
            + (f"。第一条错误：{download.errors[0]}" if download.errors else "")
        )
        ok = not download.failed
        append_fetch_log(
            settings.cache_dir,
            {
                "at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started)),
                "seconds": round(time.time() - started, 1),
                "scope": "图片索引",
                "note": f"另从图片 CDN 下载原图 {download.requests} 次请求",
                **fetcher.stats.as_dict(),
            },
        )
    (output_dir / STATS_FILE).write_text(
        json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return 0 if ok else 1


# ── embed ─────────────────────────────────────────────────────────────


def _embed(
    args: argparse.Namespace, config: AppConfig, settings: Settings, images: ImageSettings
) -> int:
    found = _provider_and_paths(args, config, settings)
    if found is None:
        return 2
    provider, images_path, store_dir = found
    cache_dir = settings.cache_dir
    try:
        result = asyncio.run(
            embed_image_files(provider, images_path, store_dir, cache_dir, images, limit=args.limit)
        )
    except VectorStoreError as exc:
        _say(str(exc))
        return 2
    report = result.report
    have = report.already + report.embedded
    _say(
        f"{provider.fingerprint}（预处理 {images.preprocess}）：该算 {report.total} 张，"
        f"已有 {have} 张（这次算了 {report.embedded} 张，用了 {report.prompt_tokens} 个 token、"
        f"{report.seconds / 60:.1f} 分钟），还缺 {report.missing} 张；"
        f"文件里另有 {report.stale} 条不属于当前图片的旧向量。向量在 {store_dir}"
    )
    if result.no_original:
        _say(
            f"有 {len(result.no_original)} 张图的原图不在缓存里（{result.no_original[0]} 等），"
            "没法算：先跑 images collect 把原图下载下来"
        )
    if report.failed:
        reason = "连续失败，已提前停下" if report.stopped else "其余照常算完"
        _say(
            f"有 {len(report.failed)} 张没算成（{reason}），重跑一次只会补这些。"
            f"第一条错误：{report.errors[0]}"
        )
    ok = not report.failed and not result.no_original
    if args.verify > 0 and have > 0:
        try:
            check = asyncio.run(
                verify_image_vectors(
                    provider, images_path, store_dir, cache_dir, images, sample=args.verify
                )
            )
        except EmbeddingError as exc:
            _say(f"抽查没做成：{exc}")
            return 1
        passed = check.lowest >= VERIFY_MIN_SIMILARITY
        _say(
            f"抽查 {check.checked} 张（每张单独重算，和存下来的比）：相似度平均 {check.mean:.4f}，"
            f"最低 {check.lowest:.4f}（{check.lowest_chunk_id}）"
            + ("" if passed else f"，低于 {VERIFY_MIN_SIMILARITY}，向量和图片可能没对上，需要查")
        )
        ok = ok and passed
    return 0 if ok else 1


# ── load / status ─────────────────────────────────────────────────────


async def _database_part(
    dsn: str,
    provider: EmbeddingProvider,
    images_path: Path,
    store_dir: Path,
    preprocess: str,
    *,
    wiki: str,
    primary: bool,
    load: bool,
) -> None:
    db = Database(dsn, pool_max=2)
    await db.connect()
    try:
        await db.migrate()
        store = PostgresImageStore(db)
        if load:
            report = await load_images_into(
                store,
                images_path,
                store_dir,
                provider,
                preprocess,
                wiki=wiki,
                replace_unlabelled=primary,
            )
            _say(
                f"导入完成：记录文件里 {report.total} 张，其中 {report.with_vectors} 张有向量；"
                f"写入 {report.upserted} 张，没变 {report.unchanged} 张，删除 {report.deleted} 张；"
                f"用时 {report.seconds:.0f} 秒"
            )
            if report.with_vectors < report.total:
                _say(
                    f"有 {report.total - report.with_vectors} 张没有向量（以图搜图找不到它们）："
                    "先跑 images embed 补上再导入一次"
                )
        info = (await store.info()).wikis.get(wiki) or ImageStoreInfo()
        _say(
            f"库里这个 wiki 现在有 {info.images} 张图，{info.with_vectors} 张有向量，"
            f"向量指纹 {info.fingerprint}，预处理 {info.preprocess}，最近一次改动 {info.loaded_at}"
        )
    finally:
        await db.close()


def _load(
    args: argparse.Namespace, config: AppConfig, settings: Settings, images: ImageSettings
) -> int:
    found = _provider_and_paths(args, config, settings)
    if found is None:
        return 2
    provider, images_path, store_dir = found
    if not config.postgres_dsn:
        _say(
            "没有数据库地址（环境变量 JIANSHANAGENT_POSTGRES_DSN 或 secrets.yaml 的 postgres.dsn）"
        )
        return 2
    try:
        asyncio.run(
            _database_part(
                config.postgres_dsn,
                provider,
                images_path,
                store_dir,
                images.preprocess,
                wiki=settings.wiki.id,
                primary=is_primary(config, settings),
                load=True,
            )
        )
    except VectorStoreError as exc:
        _say(str(exc))
        return 2
    return 0


def _status(
    args: argparse.Namespace, config: AppConfig, settings: Settings, images: ImageSettings
) -> int:
    found = _provider_and_paths(args, config, settings)
    if found is None:
        return 2
    provider, images_path, store_dir = found
    try:
        status = image_status(provider, images_path, store_dir, settings.cache_dir, images)
    except VectorStoreError as exc:
        _say(str(exc))
        return 2
    vectors = status.vectors
    kinds = "、".join(f"{kind} {count}" for kind, count in status.by_kind.items())
    _say(f"记录：{vectors.total} 张（{kinds}），在 {images_path}")
    _say(
        f"原图缓存：缺 {status.no_original} 张（在 {settings.cache_dir}，缺的跑 images collect 补）"
    )
    _say(
        f"向量（{provider.fingerprint}，预处理 {images.preprocess}）：已有 {vectors.already} 张，"
        f"还缺 {vectors.missing} 张，另有 {vectors.stale} 条不属于当前图片的旧向量。在 {store_dir}"
    )
    if not config.postgres_dsn:
        _say("数据库：没有配置数据库地址，没看")
        return 0
    asyncio.run(
        _database_part(
            config.postgres_dsn,
            provider,
            images_path,
            store_dir,
            images.preprocess,
            wiki=settings.wiki.id,
            primary=False,
            load=False,
        )
    )
    return 0


def run(args: argparse.Namespace, config: AppConfig, settings: Settings) -> int:
    images = ImageSettings.from_config(config.plugin("knowledge").get("images") or {})
    if args.images_command == "collect":
        return asyncio.run(_collect(args, settings))
    if args.images_command == "embed":
        return _embed(args, config, settings, images)
    if args.images_command == "load":
        return _load(args, config, settings, images)
    return _status(args, config, settings, images)
