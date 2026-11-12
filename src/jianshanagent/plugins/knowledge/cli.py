"""命令行入口。

部署挂了几个 wiki，知识库就各建各的（文件在 ``data/kb/<wiki 的 id>/``）。下面的命令不指明是哪个
wiki 时，挨个对每个 wiki 做一遍；只做一个的话在子命令前面加 ``--wiki <id>``::

    python -m jianshanagent.plugins.knowledge --wiki bwiki run

::

    # 全量（取数走缓存，中断后重跑会接着取；结果写到 data/kb/<wiki 的 id>/）
    python -m jianshanagent.plugins.knowledge run

    # 不联网，只用缓存重新清洗、切片、出报告（调规则时用）
    python -m jianshanagent.plugins.knowledge run --offline

    # 只跑指定页面或 Data 文档（结果写到 data/kb/<wiki 的 id>/debug/，不动全量结果）。
    # 挂了不止一个 wiki 时要指明是哪个 wiki 的页面
    python -m jianshanagent.plugins.knowledge --wiki huiji run --print \\
        --only "为崽而战·浮世之航" --only "Data:Story/575.json"

    # 忽略缓存重新取（修订号没变但模板、模块、Data 改了的时候）
    python -m jianshanagent.plugins.knowledge run --refresh

    # 沿用上次的页面清单，只补清单里缺的部分（配置里新加了数据集时用，页面不会重新列、重新取）
    python -m jianshanagent.plugins.knowledge run --reuse-listing

    # 给切片算向量（中断后重跑会接着算，已经有的不重算；用哪一家见 config/core.embedding.yaml）
    python -m jianshanagent.plugins.knowledge embed
    python -m jianshanagent.plugins.knowledge embed --provider local    # 用本机模型（服务要先起来）
    python -m jianshanagent.plugins.knowledge embed --status            # 只看算了多少，不调接口

    # 把切片和向量导入数据库（以切片文件为准，一个 wiki 一个 wiki 地对齐，没变的不动；
    # 机器人不用停）。
    # 不指明 wiki 时，库里已经不在 core.wiki.yaml 里的 wiki 的切片会一并删掉
    python -m jianshanagent.plugins.knowledge load
    python -m jianshanagent.plugins.knowledge load --status             # 只看库里现在有什么

    # wiki 图片索引（以图搜图用），同样三步，说明见 image_cli.py
    python -m jianshanagent.plugins.knowledge images collect            # 选图、生成记录、下载原图
    python -m jianshanagent.plugins.knowledge images embed              # 给图片算向量
    python -m jianshanagent.plugins.knowledge images load               # 导入数据库
    python -m jianshanagent.plugins.knowledge images status             # 只看现状

    # 自动更新：看 wiki 改了什么，只重做变了的部分并导入数据库（上面这些步骤的增量版，见 sync.py）
    python -m jianshanagent.plugins.knowledge sync                      # 做一轮
    python -m jianshanagent.plugins.knowledge sync --plan               # 只看打算做什么
    python -m jianshanagent.plugins.knowledge sync --loop               # 一直跑（kbsync 服务）
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from jianshanagent.core.config import AppConfig, load_config
from jianshanagent.core.db import Database
from jianshanagent.core.embedding import EmbeddingError, EmbeddingProvider, EmbeddingSettings
from jianshanagent.core.logging import setup_logging
from jianshanagent.core.wiki import load_wikis
from jianshanagent.plugins.knowledge import image_cli, sync_cli
from jianshanagent.plugins.knowledge.embed import (
    VectorStoreError,
    embed_chunks,
    store_status,
    verify_store,
)
from jianshanagent.plugins.knowledge.image_store import PostgresImageStore
from jianshanagent.plugins.knowledge.load import load_into
from jianshanagent.plugins.knowledge.pipeline import ProcessResult, run_pipeline
from jianshanagent.plugins.knowledge.report import write_outputs
from jianshanagent.plugins.knowledge.settings import Settings, is_primary, load_settings
from jianshanagent.plugins.knowledge.sink import CHUNKS_FILE, JsonlSink
from jianshanagent.plugins.knowledge.store import PostgresKnowledgeStore, StoreInfo

#: 单独重算的向量和存下来的相似度低于这个值，就当作有问题
VERIFY_MIN_SIMILARITY = 0.99


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m jianshanagent.plugins.knowledge",
        description="wiki 知识库：预处理（取数 → 清洗 → 切片）、给切片算向量、导入数据库；"
        "以及以图搜图用的图片索引（images）",
    )
    parser.add_argument("--config-dir", help="配置目录（默认仓库的 config/）")
    parser.add_argument(
        "--wiki",
        metavar="ID",
        help="只做这个 wiki（core.wiki.yaml 的 wikis 里的 id）；不写就挨个做每个 wiki",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="跑管线（默认全量；可断点续跑）")
    run.add_argument(
        "--only",
        action="append",
        default=[],
        metavar="标题",
        help="只处理这个页面或 Data 文档，可重复；结果写到 <这个 wiki 的目录>/debug/",
    )
    run.add_argument("--offline", action="store_true", help="不发任何请求，只用本地缓存")
    run.add_argument("--refresh", action="store_true", help="忽略缓存，重新取原始内容")
    run.add_argument(
        "--reuse-listing",
        action="store_true",
        help="沿用上次的页面清单，只补清单里缺的数据集（不重新列页面）",
    )
    run.add_argument("--print", dest="show", action="store_true", help="把切片全文打印出来")
    run.add_argument("--output-dir", help="输出目录（默认是知识库目录下这个 wiki 的那一层）")

    embed = sub.add_parser("embed", help="给切片算向量（可断点续跑）")
    embed.add_argument(
        "--provider", help="用哪一家（默认取 core.embedding.yaml 的 default_provider）"
    )
    embed.add_argument("--limit", type=int, metavar="N", help="这次最多算 N 片（试跑用）")
    embed.add_argument(
        "--verify",
        type=int,
        default=20,
        metavar="N",
        help="算完后随机抽 N 片单独重算，和存下来的比对（默认 20；0 = 不比对）",
    )
    embed.add_argument("--status", action="store_true", help="只看算了多少，不调接口")
    embed.add_argument(
        "--output-dir", help="切片所在的目录（默认是知识库目录下这个 wiki 的那一层）"
    )

    load = sub.add_parser("load", help="把切片和向量导入数据库（可反复跑）")
    load.add_argument(
        "--provider", help="用哪一家的向量（默认取 core.embedding.yaml 的 default_provider）"
    )
    load.add_argument("--status", action="store_true", help="只看库里现在有什么，不导入")
    load.add_argument("--output-dir", help="切片所在的目录（默认是知识库目录下这个 wiki 的那一层）")
    image_cli.add_parser(sub)
    sync_cli.add_parser(sub)
    return parser


def _print_chunks(result: ProcessResult) -> None:
    for chunk in result.chunks:
        print(f"\n{'─' * 20} {chunk.chunk_id}（{len(chunk.body)} 字）{'─' * 20}")
        print(chunk.text)


def _provider_and_paths(
    args: argparse.Namespace, config: AppConfig, settings: Settings
) -> tuple[EmbeddingProvider, Path, Path] | None:
    """（提供方，切片文件，这一家的向量目录）；配置不对或没有切片文件时说明原因并返回 None。"""
    embedding = EmbeddingSettings.from_config(config)
    if embedding is None:
        print("core.embedding.yaml 没有启用（enabled: false）", file=sys.stderr)
        return None
    try:
        provider = embedding.provider(args.provider)
    except EmbeddingError as exc:
        print(str(exc), file=sys.stderr)
        return None
    output_dir = Path(args.output_dir).resolve() if args.output_dir else settings.output_dir
    chunks_path = output_dir / CHUNKS_FILE
    vectors_dir = output_dir / "vectors" if args.output_dir else settings.vectors_dir
    if not chunks_path.exists():
        print(f"找不到切片文件 {chunks_path}，先跑 run", file=sys.stderr)
        return None
    return provider, chunks_path, vectors_dir / provider.name


async def _load_async(
    dsn: str,
    provider: EmbeddingProvider,
    chunks_path: Path,
    store_dir: Path,
    *,
    wiki: str,
    primary: bool,
    status_only: bool,
) -> None:
    db = Database(dsn, pool_max=2)
    await db.connect()
    try:
        await db.migrate()
        store = PostgresKnowledgeStore(db)
        if not status_only:
            report = await load_into(
                store, chunks_path, store_dir, provider, wiki=wiki, replace_unlabelled=primary
            )
            print(
                f"导入完成：切片文件里 {report.total} 片，其中 {report.with_vectors} 片有向量；"
                f"写入 {report.upserted} 片，没变 {report.unchanged} 片，删除 {report.deleted} 片；"
                f"词表 {report.terms} 个词；用时 {report.seconds:.0f} 秒",
                file=sys.stderr,
            )
            if report.with_vectors < report.total:
                print(
                    f"有 {report.total - report.with_vectors} 片没有向量（只能被关键词查到）。"
                    "不该算向量的切片本来就没有；其余的先跑 embed 补上再导入一次",
                    file=sys.stderr,
                )
        info = (await store.info()).wikis.get(wiki) or StoreInfo()
        print(
            f"库里这个 wiki 现在有 {info.chunks} 片，{info.with_vectors} 片有向量，"
            f"向量指纹 {info.fingerprint}，上次导入 {info.loaded_at}",
            file=sys.stderr,
        )
    finally:
        await db.close()


async def _forget_other_wikis(dsn: str, wikis: list[str]) -> None:
    """库里已经不在 ``core.wiki.yaml`` 里的 wiki 的切片和图片：删掉。"""
    db = Database(dsn, pool_max=2)
    await db.connect()
    try:
        await db.migrate()
        chunks = await PostgresKnowledgeStore(db).keep_only(wikis)
        images = await PostgresImageStore(db).keep_only(wikis)
    finally:
        await db.close()
    if chunks or images:
        print(
            f"库里有已经不在 core.wiki.yaml 里的 wiki 的内容，删掉了：{chunks} 片、{images} 张图",
            file=sys.stderr,
        )


def _load(args: argparse.Namespace, config: AppConfig, settings: Settings) -> int:
    found = _provider_and_paths(args, config, settings)
    if found is None:
        return 2
    provider, chunks_path, store_dir = found
    if not config.postgres_dsn:
        print(
            "没有数据库地址（环境变量 JIANSHANAGENT_POSTGRES_DSN 或 secrets.yaml 的 postgres.dsn）",
            file=sys.stderr,
        )
        return 2
    try:
        asyncio.run(
            _load_async(
                config.postgres_dsn,
                provider,
                chunks_path,
                store_dir,
                wiki=settings.wiki.id,
                primary=is_primary(config, settings),
                status_only=args.status,
            )
        )
    except VectorStoreError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


def _embed(args: argparse.Namespace, config: AppConfig, settings: Settings) -> int:
    found = _provider_and_paths(args, config, settings)
    if found is None:
        return 2
    provider, chunks_path, store_dir = found

    try:
        if args.status:
            report = store_status(provider, chunks_path, store_dir)
        else:
            report = asyncio.run(embed_chunks(provider, chunks_path, store_dir, limit=args.limit))
    except VectorStoreError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    have = report.already + report.embedded
    print(
        f"{provider.fingerprint}：该算 {report.total} 片，已有 {have} 片"
        f"（这次算了 {report.embedded} 片，用了 {report.prompt_tokens} 个 token、"
        f"{report.seconds / 60:.1f} 分钟），还缺 {report.missing} 片；"
        f"文件里另有 {report.stale} 条不属于当前切片的旧向量。向量在 {store_dir}",
        file=sys.stderr,
    )
    if report.failed:
        reason = "连续失败，已提前停下" if report.stopped else "其余照常算完"
        print(
            f"有 {len(report.failed)} 片没算成（{reason}），重跑一次只会补这些。"
            f"第一条错误：{report.errors[0]}",
            file=sys.stderr,
        )
    ok = not report.failed
    if not args.status and args.verify > 0 and have > 0:
        try:
            check = asyncio.run(verify_store(provider, chunks_path, store_dir, sample=args.verify))
        except EmbeddingError as exc:
            print(f"抽查没做成：{exc}", file=sys.stderr)
            return 1
        passed = check.lowest >= VERIFY_MIN_SIMILARITY
        print(
            f"抽查 {check.checked} 片（每片单独重算，和存下来的比）：相似度平均 {check.mean:.4f}，"
            f"最低 {check.lowest:.4f}（{check.lowest_chunk_id}）"
            + ("" if passed else f"，低于 {VERIFY_MIN_SIMILARITY}，向量和切片可能没对上，需要查"),
            file=sys.stderr,
        )
        ok = ok and passed
    return 0 if ok else 1


def _run(args: argparse.Namespace, settings: Settings) -> int:
    only = set(args.only) or None
    output_dir = Path(args.output_dir).resolve() if args.output_dir else settings.output_dir
    if only is not None and not args.output_dir:
        output_dir = output_dir / "debug"  # 调试结果不覆盖全量结果

    result = asyncio.run(
        run_pipeline(
            settings,
            JsonlSink(output_dir),
            offline=args.offline,
            refresh=args.refresh,
            only_titles=only,
            reuse_listing=args.reuse_listing,
        )
    )
    rows = write_outputs(output_dir, result, settings)
    if args.show:
        _print_chunks(result)
    by_type = result.stats["chunks_after_dedupe"]
    print(
        f"完成：{len(result.records)} 条文档记录，{len(result.chunks)} 个切片 {by_type}，"
        f"「活动页缺年份」表格 {rows} 行，输出在 {output_dir}",
        file=sys.stderr,
    )
    return 0


def _one_wiki(args: argparse.Namespace, config: AppConfig, settings: Settings) -> int:
    """对一个 wiki 做这条命令。站点插件的补充只对它那个 wiki 生效，
    所以 ``config`` 是它那一份配置，``settings`` 是它的知识库设置。"""
    if args.command == "embed":
        return _embed(args, config, settings)
    if args.command == "load":
        return _load(args, config, settings)
    if args.command == "images":
        return image_cli.run(args, config, settings)
    if args.command == "sync":
        return sync_cli.run(args, config, settings)
    return _run(args, settings)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    # 一直跑的那个是服务器上的容器，日志由 Loki 收，和机器人一样打成 JSON；其余是人在终端里跑的
    setup_logging(json_logs=args.command == "sync" and args.loop)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # 不逐条打印请求
    config = load_config(args.config_dir)
    if args.command == "sync" and (args.loop or args.health):
        return sync_cli.run_service(args, config)  # 一个进程管全部 wiki
    try:
        wikis = [config.wiki(args.wiki).id] if args.wiki else [w.id for w in load_wikis(config)]
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    one_only = [
        option
        for option, given in (
            ("--only", getattr(args, "only", None)),
            ("--output-dir", getattr(args, "output_dir", None)),
        )
        if given
    ]
    if one_only and len(wikis) > 1:
        print(
            f"挂了不止一个 wiki（{'、'.join(wikis)}），用 {'、'.join(one_only)} 时"
            "要在子命令前面加 --wiki <id> 指明是哪一个",
            file=sys.stderr,
        )
        return 2
    try:
        settings = {wiki: load_settings(config, wiki) for wiki in wikis}
    except ValueError as exc:  # 配置写得不对：说清楚哪里不对，不甩一屏调用栈
        print(str(exc), file=sys.stderr)
        return 2
    codes: list[int] = []
    for wiki in wikis:
        if len(wikis) > 1:
            print(f"── {wiki} ──", file=sys.stderr)
        codes.append(_one_wiki(args, config.for_wiki(wiki), settings[wiki]))
    loading = args.command == "load" or (args.command == "images" and args.images_command == "load")
    if loading and not args.wiki and not getattr(args, "status", False) and config.postgres_dsn:
        asyncio.run(_forget_other_wikis(config.postgres_dsn, wikis))
    return max(codes)
