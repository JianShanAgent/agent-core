"""自动更新：定时看 wiki 改了什么，只重做变了的部分，再导入数据库。

::

    # 做一轮
    python -m jianshanagent.plugins.knowledge sync
    # 一直跑，每隔 sync.interval_minutes 做一轮（服务器上的 kbsync 服务跑的就是它）
    python -m jianshanagent.plugins.knowledge sync --loop
    # 只看这一轮打算做什么（一个请求），不动任何东西
    python -m jianshanagent.plugins.knowledge sync --plan
    # 这一轮就把渲染的页面全部重取（不等到夜里）
    python -m jianshanagent.plugins.knowledge sync --full
    # 这一轮不过闸门（确认过拦得没道理时用）
    python -m jianshanagent.plugins.knowledge sync --force
    # 只看现状，不发请求
    python -m jianshanagent.plugins.knowledge sync --status

部署挂了几个 wiki 时，一个 wiki 一个 wiki 地做：各有各的游标、闸门和状态，互不耽误
（一个 wiki 出了问题，别的照常更新）。``--loop`` 是一个进程轮流管全部 wiki；
其余几种不指明就挨个做，只做一个的话在 ``sync`` 前面加 ``--wiki <id>``（见 cli.py）。

一个 wiki 一轮做的事（:func:`sync_once`）：

1. 读最近更改（changes.py），一个请求。没有相关的改动、也没有上一轮留下没做完的事，到此为止。
2. 知识库：跑一遍管线（pipeline.py）。重新列页面（6 个请求上下），只取有新修订的、渲染结果失效了的、
   被点名现渲染的，其余全走缓存；清洗和切片整体重算（几秒钟）。切片的 ID 由内容决定，
   没变的切片向量不用重算：只给新切片算向量，再把库里这个 wiki 的切片对齐成文件里的
   （没变的行不动）。
   机器人不用停，每次查询都是直接读库的，导入完马上生效。
3. 图库：重新列文件、生成记录；只给还没有向量的图片下载原图、算向量；导入数据库。
4. 状态存盘（这个 wiki 的缓存目录下的 ``sync_state.json``），并写进数据库的 ``kb_meta``
   （``sync:<wiki 的 id>``），管理后台从那里读。

全部重取（把渲染的页面不看缓存重取一遍，阴阳师 wiki 是 400 多个请求、三四分钟）是兜底：
有的页面是拿页面以外的公共数据渲染的（灰机的 Data 数据集），数据改了，页面既没有新修订、
``touched`` 也不一定变，没法知道是哪几页。哪些改动算「公共数据改了」由站点插件认（changes.py）。
所以公共数据改动满一天以后（wiki 自己的渲染缓存一天一换，早了取到的还是旧的）全部重取一次；
再怎么没动静，也每隔几天做一次。只在夜里的时段做（``sync.full_refresh``）。

没人盯着，所以每一步都有闸门（``sync.limits``）——宁可停下来等人看，也不把库改坏：

- 这台机器上没有这个 wiki 的知识库文件：不做。自动更新只做增量，不会自己从头全量取数。
- 一轮向 wiki 发的请求有上限。到了上限还没取的页面沿用旧内容，下一轮接着取。
- wiki 出故障（连着几个请求都没成）：这一轮不再发请求，没取的页面同样沿用旧内容，下一轮再说。
- 页面取不下来：缓存里有旧内容就用旧的（sources.py），不让这一页从库里消失；
  连旧内容都没有的失败太多，或者文档数、切片数、图片数比上一次少了一截：结果不写出去。
- 要新算向量的切片、要新下载的图片多得不正常（多半是向量文件没拷过来）：不算、不下、也不导入。
- 库里这个 wiki 的向量的指纹和现在配置的嵌入接口不是同一种：不导入（导进去会把库里的向量清掉）。

被闸门拦下的事记在状态里（管理后台看得到），下一轮还会再试；确认拦得没道理就手动 ``--force`` 一次。
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import fcntl
import hashlib
import json
import os
import signal
import sys
import time
from collections.abc import Awaitable, Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import httpx

from jianshanagent._bootstrap import running_version
from jianshanagent.core.config import AppConfig, load_config
from jianshanagent.core.db import Database
from jianshanagent.core.embedding import EmbeddingProvider, EmbeddingSettings
from jianshanagent.core.logging import get_logger
from jianshanagent.core.update import restart_needed
from jianshanagent.core.wiki import load_wikis
from jianshanagent.plugins.knowledge.changes import (
    ChangeContext,
    Cursor,
    Plan,
    plan_changes,
    read_changes,
)
from jianshanagent.plugins.knowledge.documents import STATUS_OK
from jianshanagent.plugins.knowledge.embed import INDEX_FILE, embed_chunks, store_status
from jianshanagent.plugins.knowledge.fetcher import DiskCache, FetchError, WikiFetcher
from jianshanagent.plugins.knowledge.image_cli import STATS_FILE as IMAGES_STATS_FILE
from jianshanagent.plugins.knowledge.image_collect import (
    IMAGES_FILE,
    collect_catalog,
    download_originals,
    read_images,
    write_images,
)
from jianshanagent.plugins.knowledge.image_embed import (
    embed_image_files,
    images_without_vectors,
    load_images_into,
    vectors_dirname,
)
from jianshanagent.plugins.knowledge.image_store import ImageStore, PostgresImageStore
from jianshanagent.plugins.knowledge.images import ImageSettings
from jianshanagent.plugins.knowledge.load import load_into
from jianshanagent.plugins.knowledge.pipeline import (
    STATUS_FETCH_FAILED,
    Gate,
    Held,
    ProcessResult,
    append_fetch_log,
    run_pipeline,
)
from jianshanagent.plugins.knowledge.report import write_outputs
from jianshanagent.plugins.knowledge.settings import (
    Settings,
    SyncSettings,
    is_primary,
    kb_root,
    load_settings,
)
from jianshanagent.plugins.knowledge.sink import CHUNKS_FILE, DOCUMENTS_FILE, JsonlSink
from jianshanagent.plugins.knowledge.sources import WikiSource, wiki_time
from jianshanagent.plugins.knowledge.store import (
    SYNC_STATUS_KEY,
    KnowledgeStore,
    PostgresKnowledgeStore,
    meta_key,
)

logger = get_logger(__name__)

STATE_FILE = "sync_state.json"
LOCK_FILE = "sync.lock"
HEARTBEAT_FILE = "sync_heartbeat"
#: 状态里留最近多少轮的记录（只记做了事或出了问题的轮次）
ROUNDS_KEPT = 30
#: ``--loop`` 每隔多少秒在心跳文件上点一下；容器的健康检查看它多久没动
HEARTBEAT_SECONDS = 30.0
HEARTBEAT_STALE_SECONDS = 300.0
#: 第一次跑时，从页面清单取下来的那一刻再往前多读这么多秒的最近更改
_FIRST_RUN_MARGIN_SECONDS = 3600.0
#: 连数据库最多等多久
_CONNECT_TIMEOUT_SECONDS = 20.0
#: 一轮里连着这么多个请求都没成（wiki 出故障、放行密钥失效），这一轮就不再向 wiki 发请求了
_GIVE_UP_AFTER = 5

RESULT_UNCHANGED = "unchanged"  # wiki 没有相关的改动，什么都没做
RESULT_CHECKED = "checked"  # 重新对过了，内容没变
RESULT_UPDATED = "updated"  # 内容有更新
RESULT_PROBLEM = "problem"  # 有步骤没做成（出错，或者被闸门拦下）


class Busy(RuntimeError):
    """另一轮自动更新还在跑。"""


# ── 状态 ──────────────────────────────────────────────────────────────


@dataclass(slots=True)
class SyncState:
    """一轮结束时存盘、下一轮开始时读回来的东西。"""

    #: 最近更改读到哪里了
    cursor_timestamp: str = ""
    cursor_rcid: int = 0
    #: 还没做完的事：从最近更改里看出来要做、还没做成的（做成了就清掉）
    kb_pending: bool = False
    embed_pending: bool = False
    images_pending: bool = False
    images_embed_pending: bool = False
    #: 内容来自的 Data 文档改了、还没按源码现渲染成的页面
    fresh_titles: list[str] = field(default_factory=list)
    #: 公共数据集最近一次改动的时间（接口的写法）；被一次全部重取盖住以后清掉
    shared_data_at: str = ""
    #: 上一次全部重取做完是什么时候；做到一半还没做完的那一次是什么时候开始的
    full_at: float = 0.0
    full_started_at: float = 0.0
    #: 上一次对图库是什么时候
    images_at: float = 0.0
    #: 上一次检查、上一次内容有更新是什么时候
    checked_at: float = 0.0
    updated_at: float = 0.0
    #: 各部分没做成的原因；做成了就是空的
    check_problem: str = ""
    kb_problem: str = ""
    images_problem: str = ""
    #: 没有变化的那些轮次发的请求，攒着记进下一条取数记录
    idle_rounds: int = 0
    idle_requests: int = 0
    #: 最近几轮的记录
    rounds: list[dict[str, Any]] = field(default_factory=list)


def load_state(cache_dir: Path) -> SyncState:
    try:
        raw = json.loads((cache_dir / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return SyncState()
    known = {f.name for f in dataclasses.fields(SyncState)}
    return (
        SyncState(**{k: v for k, v in raw.items() if k in known})
        if isinstance(raw, dict)
        else SyncState()
    )


def save_state(cache_dir: Path, state: SyncState) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / STATE_FILE
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(dataclasses.asdict(state), ensure_ascii=False, indent=1), encoding="utf-8"
    )
    os.replace(tmp, path)


def _stamp(moment: float) -> str | None:
    """时间戳 → 带时区的本地时间（和库里别的时间一个写法）；0 表示没有过。"""
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(moment)) if moment else None


def _wiki_stamp(moment: float) -> str:
    """时间戳 → 接口的写法（UTC）。"""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(moment))


def status_of(state: SyncState, cfg: SyncSettings, *, loaded: dict[str, str]) -> dict[str, Any]:
    """给管理后台看的状态（存进 ``kb_meta``）。``loaded``：库里现在装的是哪一版文件。"""
    shared = wiki_time(state.shared_data_at)
    return {
        "enabled": cfg.enabled,
        "interval_seconds": cfg.interval_seconds,
        "checked_at": _stamp(state.checked_at),
        "updated_at": _stamp(state.updated_at),
        "full_at": _stamp(state.full_at),
        "images_at": _stamp(state.images_at),
        "problems": {
            "check": state.check_problem,
            "kb": state.kb_problem,
            "images": state.images_problem,
        },
        "pending": {
            "kb": state.kb_pending or state.embed_pending,
            "images": state.images_pending or state.images_embed_pending,
            "fresh_titles": state.fresh_titles[:20],
            "shared_data_at": _stamp(shared) if shared else None,
            "full_started_at": _stamp(state.full_started_at),
        },
        "rounds": state.rounds[-ROUNDS_KEPT:],
        "loaded": loaded,
    }


class StatusStore(Protocol):
    """状态在数据库里的那一份。"""

    async def read(self) -> dict[str, Any]: ...

    async def write(self, status: dict[str, Any]) -> None: ...


class MemoryStatusStore:
    """进程内实现：测试用。"""

    def __init__(self) -> None:
        self.status: dict[str, Any] = {}

    async def read(self) -> dict[str, Any]:
        return dict(self.status)

    async def write(self, status: dict[str, Any]) -> None:
        self.status = dict(status)


class PostgresStatusStore:
    """落在 ``kb_meta`` 表里这个 wiki 的那一行（``sync:<wiki 的 id>``）。"""

    def __init__(self, db: Database, wiki: str = ""):
        self._db = db
        self._key = meta_key(SYNC_STATUS_KEY, wiki)

    async def read(self) -> dict[str, Any]:
        raw = await self._db.pool.fetchval("SELECT value FROM kb_meta WHERE key = $1", self._key)
        value = json.loads(raw) if raw else {}
        return value if isinstance(value, dict) else {}

    async def write(self, status: dict[str, Any]) -> None:
        await self._db.pool.execute(
            "INSERT INTO kb_meta (key, value) VALUES ($1, $2::jsonb) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            self._key,
            json.dumps(status, ensure_ascii=False),
        )


# ── 运行环境 ──────────────────────────────────────────────────────────


@dataclass(slots=True)
class Targets:
    """要导入的数据库。"""

    knowledge: KnowledgeStore
    images: ImageStore
    status: StatusStore


@dataclass(slots=True)
class SyncEnv:
    """给一个 wiki 做自动更新要用的东西。"""

    #: 这个 wiki 的知识库设置
    settings: Settings
    #: 图库的设置；None = 不管图库
    images: ImageSettings | None
    #: 算向量用哪一家；None = 没有配置嵌入接口（那就什么都做不了）
    provider: EmbeddingProvider | None
    #: None = 没有数据库：只更新文件，不导入
    targets: Targets | None = None
    #: 是不是主 wiki：库里没标出处的旧内容算它的，导入时一起换掉
    primary: bool = True
    #: 下面几个只有测试会给
    wiki_transport: httpx.AsyncBaseTransport | None = None
    embed_transport: httpx.AsyncBaseTransport | None = None
    cdn_transport: httpx.AsyncBaseTransport | None = None
    clock: Callable[[], float] = time.time


def build_env(config: AppConfig, settings: Settings | None = None, *, wiki: str = "") -> SyncEnv:
    """按配置装出 ``wiki``（不写就是主 wiki）的运行环境。

    不连数据库：``targets`` 由 :func:`run_wikis` 连上以后填。
    """
    settings = settings or load_settings(config, wiki)
    view = config.for_wiki(settings.wiki.id) if settings.wiki.id else config
    embedding = EmbeddingSettings.from_config(view)
    return SyncEnv(
        settings=settings,
        images=ImageSettings.from_config(view.plugin("knowledge").get("images") or {}),
        provider=embedding.provider() if embedding is not None else None,
        primary=is_primary(config, settings),
    )


@contextlib.contextmanager
def _locked(cache_dir: Path) -> Iterator[None]:
    """同一份缓存同一时刻只跑一轮。"""
    cache_dir.mkdir(parents=True, exist_ok=True)
    with (cache_dir / LOCK_FILE).open("ab") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise Busy("另一轮自动更新还在跑") from None
        yield


# ── 判断 ──────────────────────────────────────────────────────────────


def full_due(state: SyncState, cfg: SyncSettings, now: float) -> bool:
    """这一轮该不该把渲染的页面全部重取。只在配置的时段里做。"""
    local = time.localtime(now)
    if not cfg.in_full_window(local.tm_hour * 60 + local.tm_min):
        return False
    if state.full_started_at:
        return True  # 上次做到一半没做完：接着做
    since = now - state.full_at
    if since < cfg.full_min_interval_hours * 3600:
        return False
    if cfg.full_every_days > 0 and since >= cfg.full_every_days * 86400:
        return True
    changed = wiki_time(state.shared_data_at) if cfg.full_on_shared_data else None
    return changed is not None and now - changed >= cfg.full_settle_hours * 3600


def content_hash(path: Path) -> str | None:
    """文件内容的哈希；没有这个文件返回 None。"""
    digest = hashlib.sha256()
    try:
        with path.open("rb") as fh:
            for block in iter(lambda: fh.read(1 << 20), b""):
                digest.update(block)
    except OSError:
        return None
    return digest.hexdigest()[:24]


def file_signature(path: Path, index: Path) -> str:
    """一份输出（切片或图片记录，加上它的向量）现在是哪一版：内容的哈希 + 向量索引的大小。

    导入数据库以后把它记在库里，下次对不上就说明文件比库新（刚更新过，或者文件是从别处拷来的）。
    """
    size = index.stat().st_size if index.exists() else 0
    return f"{content_hash(path)}:{size}"


@dataclass(frozen=True, slots=True)
class Counts:
    #: 入库的文档数
    documents: int
    #: 各个切片的 ID（由内容决定：内容变了 ID 就变）
    chunk_ids: frozenset[str]


def output_counts(output_dir: Path) -> Counts | None:
    """上一次的输出里有多少篇入库的文档、有哪些切片；还没有输出返回 None。"""
    try:
        with (output_dir / DOCUMENTS_FILE).open(encoding="utf-8") as lines:
            documents = sum(1 for line in lines if json.loads(line).get("status") == STATUS_OK)
        with (output_dir / CHUNKS_FILE).open(encoding="utf-8") as lines:
            chunk_ids = frozenset(str(json.loads(line)["chunk_id"]) for line in lines)
    except (OSError, ValueError, KeyError):
        return None
    return Counts(documents, chunk_ids)


def _dropped(label: str, before: int, after: int, ratio: float) -> str | None:
    if before and after < before * (1 - ratio):
        return (
            f"{label}从 {before} 变成了 {after}，少了 {(before - after) / before:.0%}，"
            f"超过了 {ratio:.0%}（sync.limits.drop_ratio）"
        )
    return None


def kb_gate(before: Counts | None, cfg: SyncSettings) -> Gate:
    """管线的结果能不能写出去。"""

    def check(result: ProcessResult) -> str | None:
        failed = sum(1 for r in result.records if r.get("status") == STATUS_FETCH_FAILED)
        if failed > cfg.max_failures:
            return (
                f"有 {failed} 篇文档没取下来、缓存里也没有旧内容可用，"
                f"超过了 {cfg.max_failures} 篇（sync.limits.failures）"
            )
        if before is None:
            return None
        documents = sum(1 for r in result.records if r.get("status") == STATUS_OK)
        return _dropped("入库的文档", before.documents, documents, cfg.max_drop_ratio) or _dropped(
            "切片", len(before.chunk_ids), len(result.chunks), cfg.max_drop_ratio
        )

    return check


# ── 知识库 ────────────────────────────────────────────────────────────


async def _kb_pipeline(
    env: SyncEnv, fetcher: WikiFetcher, state: SyncState, *, full: bool, force: bool, now: float
) -> dict[str, Any]:
    """重新列页面、取变了的、整体重算切片。"""
    settings, cfg = env.settings, env.settings.sync
    out = settings.output_dir
    if full and not state.full_started_at:
        state.full_started_at = now
        save_state(settings.cache_dir, state)
    before = output_counts(out)
    result = await run_pipeline(
        settings,
        JsonlSink(out),
        refresh=full,
        refresh_since=state.full_started_at if full else 0.0,
        fresh_titles=state.fresh_titles,
        fetcher=fetcher,
        gate=None if force else kb_gate(before, cfg),
    )
    write_outputs(out, result, settings)
    run = result.stats["run"]
    stale = {str(item["title"]) for item in run["stale"]}
    # 没取下来、也没有旧内容可用的（新建的页面）：这一轮先不在库里
    failed = sum(1 for r in result.records if r.get("status") == STATUS_FETCH_FAILED)
    # 没取下来的下一轮接着取：点名现渲染的留在名单里，其余的靠修订号、touched 对不上自然会再取
    state.fresh_titles = sorted(title for title in state.fresh_titles if title in stale)
    state.kb_pending = bool(stale) or failed > 0
    state.embed_pending = True
    if full and not stale:
        state.full_at, state.full_started_at = now, 0.0
        # 公共数据集的改动已经过了等待期的，这一次就算盖住了；还没过的留着，到时候再来一次
        changed = wiki_time(state.shared_data_at)
        if changed is None or now - changed >= cfg.full_settle_hours * 3600:
            state.shared_data_at = ""
    old = before.chunk_ids if before is not None else frozenset()
    new = {chunk.chunk_id for chunk in result.chunks}
    return {
        "full": full,
        "renders": run["renders"],
        "stale": len(stale),
        "failed": failed,
        "documents": sum(1 for r in result.records if r.get("status") == STATUS_OK),
        "chunks": len(new),
        # 切片的 ID 由内容决定：新出现的 ID 是新内容，不见了的 ID 是没了或者被改掉的内容
        "added": len(new - old),
        "removed": len(old - new),
    }


def _kb_paths(env: SyncEnv) -> tuple[Path, Path]:
    """（切片文件，这一家的向量目录）。"""
    assert env.provider is not None
    return env.settings.output_dir / CHUNKS_FILE, env.settings.vectors_dir / env.provider.name


def _behind(env: SyncEnv, loaded: dict[str, str], key: str, path: Path, store_dir: Path) -> bool:
    """库里装的是不是比文件旧的一版（没有数据库就谈不上）。"""
    if env.targets is None:
        return False
    return loaded.get(key) != file_signature(path, store_dir / INDEX_FILE)


async def _kb_vectors(
    env: SyncEnv, state: SyncState, loaded: dict[str, str], *, force: bool
) -> dict[str, Any]:
    """给还没有向量的切片算向量。切片文件没动过、库也是最新的，就不用看。"""
    assert env.provider is not None
    cfg = env.settings.sync
    chunks_path, store_dir = _kb_paths(env)
    if not (state.embed_pending or _behind(env, loaded, "kb", chunks_path, store_dir)):
        return {}
    missing = store_status(env.provider, chunks_path, store_dir).missing
    if missing > cfg.max_embed_chunks and not force:
        raise Held(
            f"有 {missing} 片要新算向量，超过了 {cfg.max_embed_chunks} 片"
            "（sync.limits.embed_chunks）。平时一天只有几十片；这么多，"
            f"多半是向量文件（{store_dir}）没拷过来。没有算，也没有导入"
        )
    if not missing:
        state.embed_pending = False
        return {}
    report = await embed_chunks(env.provider, chunks_path, store_dir, transport=env.embed_transport)
    state.embed_pending = bool(report.failed)
    summary: dict[str, Any] = {"embedded": report.embedded, "tokens": report.prompt_tokens}
    if report.failed:
        # 不拦后面的导入：新内容先进库，没有向量的那几片靠关键词和「补成整页」也找得到
        summary["problem"] = (
            f"有 {len(report.failed)} 片的向量没算成（下一轮再算，这之前它们只能被关键词查到）："
            f"{report.errors[0]}"
        )
    return summary


def _stored_fingerprint(env: SyncEnv, wikis: dict[str, Any]) -> str | None:
    """库里这个 wiki 现有的向量的指纹（``wikis``：各个 wiki 的概况）；没有向量就是 None。

    主 wiki 还没导入过带出处的内容时，看的是库里没标出处的那些（它们这次会被换掉）。
    """
    mine = wikis.get(env.settings.wiki.id) or (wikis.get("") if env.primary else None)
    return mine.fingerprint if mine is not None else None


def _unlabelled(env: SyncEnv, wikis: dict[str, Any]) -> bool:
    """库里还有没标出处的内容、而且该由这个 wiki 换掉：文件没变也要导入一次，把出处标上。"""
    return env.primary and bool(env.settings.wiki.id) and "" in wikis


async def _kb_load(env: SyncEnv, loaded: dict[str, str], *, force: bool) -> dict[str, Any]:
    """文件比库新的话，导入数据库。"""
    assert env.provider is not None and env.targets is not None
    chunks_path, store_dir = _kb_paths(env)
    wikis = (await env.targets.knowledge.info()).wikis
    if not (_unlabelled(env, wikis) or _behind(env, loaded, "kb", chunks_path, store_dir)):
        return {}
    signature = file_signature(chunks_path, store_dir / INDEX_FILE)
    stored = _stored_fingerprint(env, wikis)
    if stored and stored != env.provider.fingerprint and not force:
        raise Held(
            f"库里向量的指纹是「{stored}」，现在配置的嵌入接口是「{env.provider.fingerprint}」，"
            "不是同一种。导进去会把库里的向量清掉，所以没有导入"
        )
    report = await load_into(
        env.targets.knowledge,
        chunks_path,
        store_dir,
        env.provider,
        wiki=env.settings.wiki.id,
        replace_unlabelled=env.primary,
    )
    loaded["kb"] = signature
    return {
        "upserted": report.upserted,
        "deleted": report.deleted,
        "without_vectors": report.total - report.with_vectors,
    }


# ── 图库 ──────────────────────────────────────────────────────────────


def _image_paths(env: SyncEnv) -> tuple[Path, Path]:
    """（图片记录文件，这一家的图片向量目录）。"""
    assert env.provider is not None
    out = env.settings.output_dir
    return out / IMAGES_FILE, out / "vectors" / vectors_dirname(env.provider)


async def _images_catalog(
    env: SyncEnv, fetcher: WikiFetcher, state: SyncState, *, force: bool, now: float
) -> dict[str, Any]:
    """重新列文件、取数据、生成图片记录（选哪些图、记录怎么写是站点插件的事）。"""
    assert env.images is not None
    settings, cfg = env.settings, env.settings.sync
    images_path, _ = _image_paths(env)
    before = len(read_images(images_path))
    source = WikiSource(fetcher, DiskCache(settings.cache_dir), settings)
    catalog = await collect_catalog(source, settings)
    if catalog is None:
        raise Held("没有站点插件管图库，没有重新生成记录")
    if catalog.problem:
        # 依据的数据没取齐：图片会认不出是什么（被丢掉），或者名字不全
        raise Held(f"{catalog.problem}，没有重新生成记录")
    if not force:
        reason = _dropped("图片", before, len(catalog.images), cfg.max_drop_ratio)
        if reason:
            raise Held(reason)
    write_images(images_path, catalog.images)
    (settings.output_dir / IMAGES_STATS_FILE).write_text(
        json.dumps(catalog.stats, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    state.images_pending = bool(source.stale)  # 有数据沿用的是旧内容：下一轮再对一次
    state.images_embed_pending = True
    state.images_at = now
    return {"images": len(catalog.images), "stale": len(source.stale)}


async def _images_vectors(
    env: SyncEnv, state: SyncState, loaded: dict[str, str], *, force: bool
) -> dict[str, Any]:
    """只给还没有向量的图片下载原图、算向量。记录没动过、库也是最新的，就不用看。"""
    assert env.images is not None and env.provider is not None
    settings, cfg, images = env.settings, env.settings.sync, env.images
    images_path, store_dir = _image_paths(env)
    if not (state.images_embed_pending or _behind(env, loaded, "images", images_path, store_dir)):
        return {}
    need = images_without_vectors(env.provider, read_images(images_path), store_dir, images)
    if len(need) > cfg.max_new_images and not force:
        raise Held(
            f"有 {len(need)} 张图要新算向量，超过了 {cfg.max_new_images} 张"
            "（sync.limits.new_images）。这么多，"
            f"多半是图片的向量文件（{store_dir}）没拷过来。没有下载，也没有导入"
        )
    if not need:
        state.images_embed_pending = False
        return {}
    download = await download_originals(
        need, settings.cache_dir, settings.wiki, settings.fetch, transport=env.cdn_transport
    )
    result = await embed_image_files(
        env.provider,
        images_path,
        store_dir,
        settings.cache_dir,
        images,
        transport=env.embed_transport,
    )
    left = len(result.report.failed) + len(result.no_original)
    state.images_embed_pending = bool(left)
    summary: dict[str, Any] = {
        "downloaded": download.downloaded,
        "embedded": result.report.embedded,
    }
    if left:
        errors = [*download.errors, *result.report.errors]
        summary["problem"] = (
            f"有 {left} 张图没算成向量（下一轮再试，这之前以图搜图比不到它们）："
            f"{errors[0] if errors else '原图不在缓存里'}"
        )
    return summary


async def _images_load(env: SyncEnv, loaded: dict[str, str], *, force: bool) -> dict[str, Any]:
    assert env.images is not None and env.provider is not None and env.targets is not None
    images_path, store_dir = _image_paths(env)
    wikis = (await env.targets.images.info()).wikis
    if not (_unlabelled(env, wikis) or _behind(env, loaded, "images", images_path, store_dir)):
        return {}
    signature = file_signature(images_path, store_dir / INDEX_FILE)
    stored = _stored_fingerprint(env, wikis)
    if stored and stored != env.provider.fingerprint and not force:
        raise Held(
            f"库里图片向量的指纹是「{stored}」，现在配置的嵌入接口是"
            f"「{env.provider.fingerprint}」，不是同一种。导进去会把库里的向量清掉，所以没有导入"
        )
    report = await load_images_into(
        env.targets.images,
        images_path,
        store_dir,
        env.provider,
        env.images.preprocess,
        wiki=env.settings.wiki.id,
        replace_unlabelled=env.primary,
    )
    loaded["images"] = signature
    return {
        "upserted": report.upserted,
        "deleted": report.deleted,
        "without_vectors": report.total - report.with_vectors,
    }


# ── 一轮 ──────────────────────────────────────────────────────────────

Step = Callable[[], Awaitable[dict[str, Any]]]


async def _run_steps(name: str, steps: list[Step]) -> tuple[dict[str, Any], str]:
    """挨个做一个部分（知识库或图库）的几步：（各步的结果合在一起，没做成的原因）。

    哪一步被闸门拦下或者出了错，后面的就不做了，原因记下来；别的部分照常做。
    一步做成了但留了尾巴（结果里带 ``problem``，比如有几片向量没算成）的，接着往下做。
    """
    summary: dict[str, Any] = {}
    problems: list[str] = []
    for step in steps:
        try:
            part = await step()
        except Held as exc:
            logger.error("kb_sync_held", part=name, reason=str(exc))
            problems.append(str(exc))
            break
        except Exception as exc:  # 一轮里哪一步出错都不能让整个循环停下来
            logger.exception("kb_sync_step_failed", part=name)
            problems.append(f"{type(exc).__name__}: {exc}"[:500])
            break
        note = part.pop("problem", "")
        if note:
            problems.append(str(note))
        summary |= part
    return summary, "；".join(problems)


def _first_run_since(cache: DiskCache, cfg: SyncSettings, now: float) -> str:
    """第一次跑从什么时候的最近更改读起：页面清单取下来的那一刻（缓存就是那时候的样子）。"""
    listing = cache.get_named("listing.json") or {}
    fetched_at = float(listing.get("fetched_at") or 0.0) - _FIRST_RUN_MARGIN_SECONDS
    return _wiki_stamp(max(fetched_at, now - cfg.lookback_days * 86400))


def _ready(env: SyncEnv) -> str:
    """这台机器能不能做自动更新：不能的话返回原因。不发请求。"""
    settings = env.settings
    if env.provider is None:
        return "没有配置嵌入接口（core.embedding.yaml 的 enabled 是 false），新内容没法算向量"
    have_listing = DiskCache(settings.cache_dir).get_named("listing.json") is not None
    if not have_listing or not (settings.output_dir / CHUNKS_FILE).exists():
        return (
            f"这台机器上还没有知识库的文件（{settings.output_dir} 下的 chunks.jsonl "
            "和缓存里的页面清单）。自动更新只做增量，不会自己从头全量取数："
            "先把开发机的知识库目录拷过来（deploy/scripts/release.sh kb-seed），"
            "或者手动跑一次 run、embed、load"
        )
    return ""


async def sync_once(
    env: SyncEnv, *, force: bool = False, full: bool = False, plan_only: bool = False
) -> dict[str, Any]:
    """做一轮，返回这一轮的记录（``result`` 是 ``RESULT_*`` 之一）。

    ``force``：不过闸门；``full``：不等到夜里，这一轮就把渲染的页面全部重取；
    ``plan_only``：只读最近更改、说出打算做什么（记录的 ``plan``），不取数、不存状态。
    """
    with _locked(env.settings.cache_dir):
        return await _sync_once(env, force=force, full=full, plan_only=plan_only)


async def _sync_once(env: SyncEnv, *, force: bool, full: bool, plan_only: bool) -> dict[str, Any]:
    settings, cfg = env.settings, env.settings.sync
    cache = DiskCache(settings.cache_dir)
    out = settings.output_dir
    started = time.perf_counter()
    now = env.clock()
    state = load_state(settings.cache_dir)
    record: dict[str, Any] = {"at": _stamp(now), "result": RESULT_UNCHANGED}
    loaded: dict[str, str] = {}
    if env.targets is not None:
        loaded = dict((await env.targets.status.read()).get("loaded") or {})
    fetcher: WikiFetcher | None = None

    async def finish() -> dict[str, Any]:
        problems = [p for p in (state.check_problem, state.kb_problem, state.images_problem) if p]
        record["problems"] = problems
        record["seconds"] = round(time.perf_counter() - started, 1)
        record["requests"] = fetcher.stats.requests if fetcher is not None else 0
        if problems:
            record["result"] = RESULT_PROBLEM
        if plan_only:
            return record
        if fetcher is not None:
            worked = "kb" in record or "images" in record
            _log_requests(settings, state, fetcher, now, record["seconds"], worked=worked)
        state.checked_at = now
        if record["result"] == RESULT_UPDATED:
            state.updated_at = now
        if record["result"] != RESULT_UNCHANGED:
            state.rounds = [*state.rounds, record][-ROUNDS_KEPT:]
        save_state(settings.cache_dir, state)
        if env.targets is not None:
            await env.targets.status.write(status_of(state, cfg, loaded=loaded))
        log = logger.error if problems else logger.info
        log("kb_sync_round", **{k: v for k, v in record.items() if k != "at"})
        return record

    state.check_problem = _ready(env)
    if state.check_problem:
        return await finish()

    fetcher = WikiFetcher(
        settings.wiki,
        settings.fetch,
        transport=env.wiki_transport,
        budget=cfg.max_requests,
        give_up_after=_GIVE_UP_AFTER,
    )
    try:
        # 1. wiki 改了什么
        try:
            feed = await read_changes(
                fetcher,
                Cursor(state.cursor_timestamp, state.cursor_rcid),
                since=_first_run_since(cache, cfg, now),
                max_requests=cfg.changes_max_requests,
            )
        except FetchError as exc:
            state.check_problem = f"读 wiki 的最近更改没成：{exc}"[:500]
            return await finish()
        images_on = (
            env.images is not None and settings.sites.has_images and (out / IMAGES_FILE).exists()
        )
        plan = plan_changes(feed.changes, settings, ChangeContext(settings, cache, images_on))
        record["changes"], record["titles"] = plan.counts, plan.titles
        do_full = full or full_due(state, cfg, now)
        images_due = images_on and now - state.images_at >= cfg.images_every_hours * 3600
        if plan_only:
            record["plan"] = _describe(
                plan, state, cfg, do_full=do_full, images_on=images_on, images_due=images_due
            )
            return await finish()
        # 记进状态马上存盘：后面哪一步断了，下一轮还知道有这些事没做
        state.cursor_timestamp, state.cursor_rcid = feed.cursor.timestamp, feed.cursor.rcid
        state.kb_pending = state.kb_pending or plan.kb or bool(plan.fresh_titles)
        state.images_pending = state.images_pending or plan.images
        state.fresh_titles = sorted({*state.fresh_titles, *plan.fresh_titles})
        state.shared_data_at = max(state.shared_data_at, plan.shared_data_at)
        save_state(settings.cache_dir, state)
        connected = env.targets is not None
        shared = fetcher  # 给下面的闭包用：这时候它一定不是 None

        # 2. 知识库：取数和切片 → 算向量 → 导入。后两步自己看要不要做（文件比库新才做）
        do_kb = state.kb_pending or do_full
        steps: list[Step] = []
        if do_kb:
            steps.append(
                lambda: _kb_pipeline(env, shared, state, full=do_full, force=force, now=now)
            )
        steps.append(lambda: _kb_vectors(env, state, loaded, force=force))
        if connected:
            steps.append(lambda: _kb_load(env, loaded, force=force))
        kb, state.kb_problem = await _run_steps("kb", steps)
        if kb:
            record["kb"] = kb
        changed = False

        # 3. 图库：同样三步
        images: dict[str, Any] = {}
        if images_on:
            do_images = state.images_pending or images_due
            before = content_hash(out / IMAGES_FILE) if do_images else None
            steps = []
            if do_images:
                steps.append(lambda: _images_catalog(env, shared, state, force=force, now=now))
            steps.append(lambda: _images_vectors(env, state, loaded, force=force))
            if connected:
                steps.append(lambda: _images_load(env, loaded, force=force))
            images, state.images_problem = await _run_steps("images", steps)
            if images:
                record["images"] = images
            changed = changed or (do_images and before != content_hash(out / IMAGES_FILE))

        changed = changed or any(
            part.get(key)
            for part in (kb, images)
            for key in ("added", "removed", "upserted", "deleted", "embedded")
        )
        if changed:
            record["result"] = RESULT_UPDATED
        elif kb or images:
            record["result"] = RESULT_CHECKED
        return await finish()
    finally:
        await fetcher.aclose()


def _log_requests(
    settings: Settings,
    state: SyncState,
    fetcher: WikiFetcher,
    started_at: float,
    seconds: float,
    *,
    worked: bool,
) -> None:
    """记进取数记录。没有变化的轮次只发一个请求，不一轮记一条，攒到下一条里一起记。"""
    stats = fetcher.stats.as_dict()
    if not worked:
        state.idle_rounds += 1
        state.idle_requests += stats["requests"]
        return
    note = ""
    if state.idle_rounds:
        note = f"其中 {state.idle_requests} 个请求是这之前 {state.idle_rounds} 轮没有变化的检查发的"
        stats["requests"] += state.idle_requests
        stats["by_kind"]["changes"] = stats["by_kind"].get("changes", 0) + state.idle_requests
        state.idle_rounds = state.idle_requests = 0
    append_fetch_log(
        settings.cache_dir,
        {
            "at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started_at)),
            "seconds": seconds,
            "scope": "自动更新",
            **({"note": note} if note else {}),
            **stats,
        },
    )


def _describe(
    plan: Plan,
    state: SyncState,
    cfg: SyncSettings,
    *,
    do_full: bool,
    images_on: bool,
    images_due: bool,
) -> list[str]:
    """``--plan``：这一轮打算做什么，一条一句。"""
    lines: list[str] = []
    fresh = sorted({*state.fresh_titles, *plan.fresh_titles})
    if state.kb_pending or plan.kb or fresh or do_full:
        why = []
        if plan.kb:
            why.append("有条目、模板或入库的 Data 文档改了")
        if state.kb_pending:
            why.append("上一轮有没做完的")
        lines.append("知识库：重新列页面，取变了的" + (f"（{'；'.join(why)}）" if why else ""))
    if fresh:
        names = "、".join(fresh[:10])
        lines.append(f"按源码现渲染 {len(fresh)} 个页面（它们的 Data 改了）：{names}")
    if do_full:
        lines.append("渲染的页面全部重取")
    changed = wiki_time(max(state.shared_data_at, plan.shared_data_at))
    if changed is not None and not do_full and cfg.full_on_shared_data:
        ready = _stamp(changed + cfg.full_settle_hours * 3600)
        lines.append(
            f"公共数据集在 {_stamp(changed)} 改过：{ready} 以后的第一个全部重取的时段再全部重取一次"
        )
    if images_on and (state.images_pending or plan.images or images_due):
        lines.append("图库：重新列文件，给新图算向量")
    return lines or ["没有要做的"]


# ── 入口 ──────────────────────────────────────────────────────────────


async def _connect(config: AppConfig) -> Database | None:
    """连数据库；没配、或者连不上就是 None（那就只更新文件）。"""
    if not config.postgres_dsn:
        return None
    db = Database(config.postgres_dsn, pool_max=2)
    try:
        await asyncio.wait_for(db.connect(), _CONNECT_TIMEOUT_SECONDS)
    except Exception as exc:
        # 库连不上不耽误更新文件：文件比库新，下一轮连上了自然会导入
        reason = f"{type(exc).__name__}: {exc}"[:300]
        logger.warning("kb_sync_database_unavailable", error=reason)
        return None
    return db


async def run_wikis(
    config: AppConfig,
    wikis: Sequence[str],
    *,
    force: bool = False,
    full: bool = False,
    plan_only: bool = False,
    keep_going: bool = False,
    forget_others: bool = False,
) -> dict[str, dict[str, Any]]:
    """按配置给 ``wikis`` 这几个 wiki 各做一轮：wiki 的 id → 这一轮的记录。

    有数据库就连上、导入，没有（或者连不上）就只更新文件。``keep_going``：一个 wiki 这一轮
    出了岔子（上一轮还没跑完、配置写坏了……）记下来接着做下一个，不抛出去。
    ``forget_others``：库里已经不在 ``core.wiki.yaml`` 里的 wiki 的内容顺手删掉。
    """
    db = None if plan_only else await _connect(config)
    records: dict[str, dict[str, Any]] = {}
    try:
        for wiki in wikis:
            try:
                env = build_env(config, wiki=wiki)
                if db is not None:
                    env.targets = Targets(
                        knowledge=PostgresKnowledgeStore(db),
                        images=PostgresImageStore(db),
                        status=PostgresStatusStore(db, env.settings.wiki.id),
                    )
                records[wiki] = await sync_once(env, force=force, full=full, plan_only=plan_only)
            except Busy:
                if not keep_going:
                    raise
                logger.warning("kb_sync_busy", wiki=wiki)
            except Exception:  # 配置写坏了、磁盘满了……：记下来，下一轮再试
                if not keep_going:
                    raise
                logger.exception("kb_sync_round_crashed", wiki=wiki)
        if db is not None and forget_others:
            keep = [entry.id for entry in config.wikis]
            chunks = await PostgresKnowledgeStore(db).keep_only(keep)
            images = await PostgresImageStore(db).keep_only(keep)
            if chunks or images:
                logger.warning("kb_sync_forgot_removed_wikis", chunks=chunks, images=images)
    finally:
        if db is not None:
            await db.close()
    return records


async def run_once(
    config: AppConfig,
    *,
    wiki: str = "",
    force: bool = False,
    full: bool = False,
    plan_only: bool = False,
) -> dict[str, Any]:
    """按配置给 ``wiki``（不写就是主 wiki）做一轮，返回这一轮的记录。"""
    wiki = wiki or load_wikis(config)[0].id
    records = await run_wikis(config, [wiki], force=force, full=full, plan_only=plan_only)
    return records[wiki]


async def run_loop(config_dir: str | None = None) -> int:
    """一直跑到收到停止信号，轮流给挂着的每个 wiki 做。

    每一轮重新读配置，所以管理后台里改的配置下一轮就生效。各个 wiki 多久做一轮、开没开
    读的是它自己那一份配置的 ``sync`` 段。
    """
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    heartbeat: Path | None = None

    async def beat() -> None:
        while True:
            if heartbeat is not None:
                with contextlib.suppress(OSError):
                    heartbeat.parent.mkdir(parents=True, exist_ok=True)
                    heartbeat.touch()
            await asyncio.sleep(HEARTBEAT_SECONDS)

    beating = loop.create_task(beat())
    interval = SyncSettings().interval_seconds
    #: 各个 wiki 上一轮是什么时候开始的
    last: dict[str, float] = {}
    logger.info("kb_sync_loop_started", version=running_version())
    try:
        while not stop.is_set():
            round_started = time.monotonic()
            try:
                config = load_config(config_dir)
                heartbeat = kb_root(config) / HEARTBEAT_FILE
                due: list[str] = []
                intervals: list[float] = []
                for entry in config.wikis:
                    cfg = load_settings(config, entry.id).sync
                    if not cfg.enabled:
                        logger.info(
                            "kb_sync_disabled",
                            wiki=entry.id,
                            hint="plugin.knowledge 的 sync.enabled",
                        )
                        continue
                    intervals.append(cfg.interval_seconds)
                    # 留一秒的余量：这一轮和上一轮的间隔本来就是按它算的，差几毫秒不该再等一整轮
                    since = round_started - last.get(entry.id, float("-inf"))
                    if since >= cfg.interval_seconds - 1:
                        due.append(entry.id)
                interval = min(intervals, default=SyncSettings().interval_seconds)
                last |= dict.fromkeys(due, round_started)
                if due:
                    await run_wikis(config, due, keep_going=True, forget_others=True)
            except Exception:  # 配置写坏了、磁盘满了……：记下来，下一轮再试，不退出
                logger.exception("kb_sync_round_crashed")
            wait = max(60.0, interval - (time.monotonic() - round_started))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), wait)
            if not stop.is_set() and restart_needed():
                # 机器人那边换了核心的版本（管理后台里点的更新）：这个进程也换过去
                logger.warning("kb_sync_restarting_for_update")
                beating.cancel()
                os.execv(sys.executable, [sys.executable, *sys.orig_argv[1:]])
    finally:
        beating.cancel()
    logger.info("kb_sync_loop_stopped")
    return 0


def healthy(root: Path, *, now: float | None = None) -> bool:
    """``--loop`` 还活着没有：知识库目录（``root``）下的心跳文件最近有没有被点过。"""
    try:
        age = (now or time.time()) - (root / HEARTBEAT_FILE).stat().st_mtime
    except OSError:
        return False
    return age < HEARTBEAT_STALE_SECONDS
