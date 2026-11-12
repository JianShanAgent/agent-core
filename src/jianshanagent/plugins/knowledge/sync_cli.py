"""``sync`` 子命令：自动更新（做法和各个参数的用处见 sync.py 顶部）。"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from typing import Any

from jianshanagent.core.config import AppConfig
from jianshanagent.plugins.knowledge.settings import Settings, kb_root
from jianshanagent.plugins.knowledge.sync import (
    RESULT_CHECKED,
    RESULT_PROBLEM,
    RESULT_UNCHANGED,
    RESULT_UPDATED,
    Busy,
    full_due,
    healthy,
    load_state,
    run_loop,
    run_once,
    status_of,
)

_RESULTS = {
    RESULT_UNCHANGED: "wiki 没有相关的改动，什么都没做",
    RESULT_CHECKED: "重新对过了，内容没有变",
    RESULT_UPDATED: "内容有更新",
    RESULT_PROBLEM: "有步骤没做成",
}
_RENDERS = {
    "revision": "有新修订",
    "touched": "模板或模块改了",
    "data": "Data 改了（现渲染）",
    "refresh": "全部重取",
}


def add_parser(sub: Any) -> None:
    sync = sub.add_parser("sync", help="自动更新：看 wiki 改了什么，只重做变了的部分，再导入数据库")
    mode = sync.add_mutually_exclusive_group()
    mode.add_argument("--loop", action="store_true", help="一直跑，每隔一段时间做一轮")
    mode.add_argument(
        "--plan", action="store_true", help="只看这一轮打算做什么（一个请求），不动任何东西"
    )
    mode.add_argument("--status", action="store_true", help="只看现状，不发请求")
    mode.add_argument("--health", action="store_true", help="--loop 还活着没有（容器的健康检查用）")
    sync.add_argument(
        "--full", action="store_true", help="这一轮就把渲染的页面全部重取，不等到夜里"
    )
    sync.add_argument("--force", action="store_true", help="这一轮不过闸门（确认过拦得没道理时用）")


def _say(text: str) -> None:
    print(text, file=sys.stderr)


def _part(name: str, part: dict[str, Any]) -> str:
    """一个部分（知识库或图库）这一轮做了什么，一句话。"""
    bits: list[str] = []
    if "documents" in part:
        bits.append(f"{part['documents']} 篇文档、{part['chunks']} 片")
        if part.get("added") or part.get("removed"):
            bits.append(f"新内容 {part['added']} 片、去掉 {part['removed']} 片")
    if "images" in part:
        bits.append(f"{part['images']} 张图")
    renders = part.get("renders") or {}
    if renders:
        detail = "、".join(f"{_RENDERS.get(k, k)} {v}" for k, v in renders.items())
        bits.append(f"重新渲染 {sum(renders.values())} 页（{detail}）")
    if part.get("stale"):
        bits.append(f"{part['stale']} 份没取下来、沿用旧内容")
    if part.get("failed"):
        bits.append(f"{part['failed']} 篇新文档没取下来、下一轮再取")
    if part.get("downloaded"):
        bits.append(f"下载原图 {part['downloaded']} 张")
    if part.get("embedded"):
        bits.append(f"新算向量 {part['embedded']} 个")
    if "upserted" in part:
        bits.append(f"入库：写入 {part['upserted']}、删除 {part['deleted']}")
    return f"{name}：" + ("；".join(bits) or "没有变化")


def _print_round(record: dict[str, Any]) -> None:
    changes = record.get("changes") or {}
    seen = "、".join(f"{kind} {count} 条" for kind, count in changes.items()) or "没有"
    _say(f"wiki 上新的改动：{seen}")
    if record.get("titles"):
        _say("  例如：" + "、".join(record["titles"]))
    for line in record.get("plan") or []:
        _say(f"  打算做：{line}")
    if "kb" in record:
        _say(_part("知识库", record["kb"]))
    if "images" in record:
        _say(_part("图库", record["images"]))
    for problem in record.get("problems") or []:
        _say(f"没做成：{problem}")
    if "plan" not in record:
        result = _RESULTS.get(record["result"], record["result"])
        requests, seconds = record.get("requests", 0), record.get("seconds", 0)
        _say(f"结果：{result}（向 wiki 发了 {requests} 个请求，用时 {seconds} 秒）")


def _print_status(settings: Settings) -> None:
    state = load_state(settings.cache_dir)
    status = status_of(state, settings.sync, loaded={})
    _say(
        f"上次检查 {status['checked_at'] or '还没跑过'}；上次内容有更新 "
        f"{status['updated_at'] or '还没有'}；上次全部重取 {status['full_at'] or '还没有'}；"
        f"上次对图库 {status['images_at'] or '还没有'}"
    )
    pending = status["pending"]
    waiting = []
    if pending["kb"]:
        waiting.append("知识库有没做完的")
    if pending["images"]:
        waiting.append("图库有没做完的")
    if pending["fresh_titles"]:
        waiting.append("等着现渲染：" + "、".join(pending["fresh_titles"]))
    if pending["shared_data_at"]:
        waiting.append(f"公共数据集在 {pending['shared_data_at']} 改过，等着全部重取")
    if pending["full_started_at"]:
        waiting.append(f"{pending['full_started_at']} 开始的全部重取还没做完")
    _say("留着没做的：" + ("；".join(waiting) or "没有"))
    if full_due(state, settings.sync, time.time()):
        _say("现在在全部重取的时段里，下一轮会全部重取")
    for name, problem in status["problems"].items():
        if problem:
            _say(f"没做成（{name}）：{problem}")
    for record in status["rounds"][-5:]:
        parts = [
            _part(label, record[key])
            for key, label in (("kb", "知识库"), ("images", "图库"))
            if key in record
        ]
        result = _RESULTS.get(record["result"], record["result"])
        _say(f"  {record['at']} {result} {'；'.join(parts)}")


def run_service(args: argparse.Namespace, config: AppConfig) -> int:
    """``--loop`` 和 ``--health``：一个进程管挂着的全部 wiki。"""
    if args.wiki:
        _say("--loop、--health 管的是全部 wiki，不能和 --wiki 一起用")
        return 2
    if args.health:
        return 0 if healthy(kb_root(config)) else 1
    return asyncio.run(run_loop(args.config_dir))


def run(args: argparse.Namespace, config: AppConfig, settings: Settings) -> int:
    """给一个 wiki（``settings`` 是它的设置）看现状、看打算做什么，或者做一轮。"""
    if args.status:
        _print_status(settings)
        return 0
    try:
        record = asyncio.run(
            run_once(
                config,
                wiki=settings.wiki.id,
                force=args.force,
                full=args.full,
                plan_only=args.plan,
            )
        )
    except Busy as exc:
        _say(str(exc))
        return 2
    _print_round(record)
    return 1 if record["result"] == RESULT_PROBLEM else 0
