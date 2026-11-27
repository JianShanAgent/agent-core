"""微博采集目标的纯配置校验；输入 URL 从不直接用于网络请求。"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from jianshanagent.core.config import AppConfig
from urllib.parse import urlsplit

_UID = re.compile(r"[1-9][0-9]{0,19}", re.ASCII)
_KEYS = frozenset(
    {
        "uid",
        "enabled",
        "initial_days",
        "overlap_days",
        "max_posts",
        "interval_seconds",
        "include_reposts",
    }
)


def parse_weibo_uid(value: Any) -> str:
    """仅接收十进制 UID 或没有额外参数的官方 HTTPS /u/UID 主页。"""
    if not isinstance(value, str):
        raise ValueError("微博目标 UID 必须是数字文本或官方 /u/UID 主页")
    text: str = value.strip()
    if any(ord(char) < 33 or ord(char) == 127 for char in text):
        raise ValueError("微博目标不能包含控制字符或空格")
    if _UID.fullmatch(text):
        return text
    try:
        parsed = urlsplit(text)
        if (
            parsed.scheme != "https"
            or parsed.netloc not in {"weibo.com", "www.weibo.com"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError
        match = re.fullmatch(r"/u/([1-9][0-9]{0,19})/?", parsed.path, re.ASCII)
        if match:
            return match[1]
    except ValueError:
        pass
    raise ValueError("微博目标只接受数字 UID 或 https://weibo.com/u/UID 官方主页")


def parse_weibo_collector(value: Any) -> dict[str, Any]:
    """整体校验并补齐默认值；拒绝凭据、网络地址、代码及其他未知项。"""
    if not isinstance(value, dict) or value.keys() - _KEYS:
        raise ValueError("collector 只能包含允许的微博采集配置")
    result: dict[str, Any] = {"uid": parse_weibo_uid(value.get("uid"))}
    for key, boolean_default in (("enabled", False), ("include_reposts", True)):
        item = value.get(key, boolean_default)
        if not isinstance(item, bool):
            raise ValueError(f"collector.{key} 必须是布尔值")
        result[key] = item
    for key, default, low, high in (
        ("initial_days", 30, 1, 365),
        ("overlap_days", 7, 1, 30),
        ("max_posts", 200, 20, 500),
        ("interval_seconds", 1800, 900, 86400),
    ):
        item = value.get(key, default)
        if type(item) is not int or not low <= item <= high:
            raise ValueError(f"collector.{key} 必须是 {low} 到 {high} 的整数")
        result[key] = item
    return result


def validate_weibo_history(config: AppConfig, source_id: str, uid: str) -> None:
    """绑定前和每次保存均核对现有导入快照；无法证实归属时要求新来源。"""
    from jianshanagent.plugins.knowledge.settings import kb_root

    def read(path: Path, limit: int) -> Any:
        if path.is_symlink() or path.stat().st_size > limit:
            raise ValueError("来源历史无法确认归属，请新建来源")
        with path.open("rb") as stream:
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise ValueError("来源历史无法确认归属，请新建来源")
        return json.loads(data)

    root = kb_root(config)
    directory = root / source_id / "source-import"
    try:
        for path in (root / source_id, directory, directory / "generations"):
            if path.is_symlink():
                raise ValueError("来源历史无法确认归属，请新建来源")
        current = directory / "current.json"
        if not current.exists():
            # A journal without a current generation is not evidence of an empty source.
            if (directory / "journal.json").exists():
                raise ValueError("来源历史正在恢复，请恢复后绑定或新建来源")
            return
        generation = read(current, 65536).get("generation")
        if not isinstance(generation, str) or not re.fullmatch(r"[0-9a-f]{32}", generation):
            raise ValueError("来源历史无法确认归属，请新建来源")
        stage = directory / "generations" / generation
        if stage.is_symlink():
            raise ValueError("来源历史无法确认归属，请新建来源")
        metadata = stage / "documents.json"
        if not metadata.exists():
            metadata = stage / "snapshot.json"
        rows = read(metadata, 32 * 1024 * 1024).get("rows")
        if not isinstance(rows, list):
            raise ValueError("来源历史无法确认归属，请新建来源")
        if any(not isinstance(row, dict) or row.get("author_id") != uid for row in rows):
            raise ValueError("已有资料作者与目标 UID 不一致或无法确认，请新建来源")
    except (OSError, TypeError, AttributeError, KeyError, RecursionError) as exc:
        raise ValueError("来源历史无法确认归属，请新建来源") from exc
