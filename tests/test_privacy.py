"""两条靠「扫一遍代码」来守的规矩：日志里不写用户说的话；配置文件里的项代码都真的在读。

都是出过事才加的：问题的前 80 个字、搜索词曾经每条都打进日志（日志进 Loki 留 14 天，
不受「用户要求删除」「到期清理」管）；配置里曾经有好几项写着能调、其实代码根本没读。
"""

import ast
import re
from pathlib import Path

import yaml

from jianshanagent.core.config import load_config

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src" / "jianshanagent"

#: 这些名字的日志字段装的是用户说的话或者从里面取出来的东西，不许出现。
#: 要记就记长度 / 个数（question_chars=len(question)），具体内容去处理记录里看
_SPOKEN = {
    "question",
    "query",
    "keywords",
    "keyword",
    "content",
    "text",
    "args",
    "reply",
    "answer",
    "prompt",
    "written",
    "resolved",
    "names",
}


def _is_count(value: ast.expr) -> bool:
    """``len(...)``、``sum(...)``、``bool(...)`` 这类：记的是数量，不是内容。"""
    return (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id in {"len", "sum", "bool", "round", "int"}
    )


def test_log_lines_do_not_carry_what_users_said() -> None:
    offenders: list[str] = []
    for path in sorted(SOURCE.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in {"logger", "log"}
            ):
                continue
            for keyword in node.keywords:
                if keyword.arg in _SPOKEN and not _is_count(keyword.value):
                    where = f"{path.relative_to(ROOT)}:{node.lineno}"
                    offenders.append(f"{where} {keyword.arg}={ast.unparse(keyword.value)}")
    assert offenders == []


#: 这几处的键不是「配置项」而是数据（wiki 页面里的样式名、数据集的名字、嵌入服务商的名字），
#: 代码是整张表拿去用的，不会一个个按名字读。写的是（配置名，这一段的路径……）
_DATA_TABLES = (
    ("core.embedding", "providers"),
    ("core.llm", "profiles"),
    ("plugin.knowledge", "clean"),
    # 站点插件对通用配置的补充（读配置时并进去）：清洗规则；人设卡片
    # （persona/ 下的文件不在这条检查里）
    ("site.huiji", "extend", "plugin.knowledge", "clean"),
    ("site.huiji_yys", "extend", "plugin.knowledge", "clean"),
    ("site.huiji_yys", "extend", "persona/persona.default"),
    ("site.huiji_yys", "knowledge", "datasets"),
)
#: 站点插件配置里的这一段，下面一层的键是「要补充的那份配置的名字」
_EXTEND = "extend"


def _is_data(name: str, keys: tuple[str, ...]) -> bool:
    """这个键是不是在某张数据表里面（表本身那一级的名字仍然要查）。"""
    return any(
        name == table[0] and len(keys) > len(table) - 1 and keys[: len(table) - 1] == table[1:]
        for table in _DATA_TABLES
    )


def _keys(node: object, path: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    found: list[tuple[str, ...]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            found.append((*path, str(key)))
            found.extend(_keys(value, (*path, str(key))))
    return found


def test_every_key_in_the_config_files_is_read_by_the_code() -> None:
    source = "\n".join(path.read_text(encoding="utf-8") for path in sorted(SOURCE.rglob("*.py")))
    unread: list[str] = []
    # 每份配置的默认值：部署级的在 config/，插件的在各自的目录里
    sources = load_config(ROOT / "tests" / "config").sources
    assert "plugin.chat" in sources and "core.wiki" in sources
    for name, path in sorted(sources.items()):
        if name.startswith("persona/"):
            continue  # 人设卡片是整张拿去用的
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for keys in _keys(data):
            if _is_data(name, keys):
                continue
            if name.startswith("site.") and len(keys) == 2 and keys[0] == _EXTEND:
                # 补充的是哪份配置：得真有这份配置
                if keys[1] not in sources:
                    unread.append(f"{name}: {'.'.join(keys)}（没有这份配置）")
                continue
            if not re.search(rf"""["']{re.escape(keys[-1])}["']""", source):
                unread.append(f"{name}: {'.'.join(keys)}")
    # 代码里连这个键的名字都没出现过 = 改了它什么都不会变。要么接上，要么从配置里删掉
    assert unread == []
