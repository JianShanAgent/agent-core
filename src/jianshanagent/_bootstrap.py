"""启动引导：让数据目录里的「更新包」盖住环境里装的核心。

核心是装在 Python 环境里的（``uv sync`` 装的，或者构建镜像时装的）。在管理后台点「更新」时，
新版本不动这个环境——容器里它是镜像的一部分，重建容器就没了；``uv run`` 每次也会把环境对回锁文件——
而是解到数据目录下::

    <更新目录>/                     默认是实例目录的 data/core，容器里是 /data/core
      state.json                   现在用哪个版本、它确认能起来了没有（格式见 read_state）
      versions/<版本>/jianshanagent/  那个版本的包

``jianshanagent/__init__.py`` 一被导入就调 :func:`activate`：有确认过能起来的更新包、
而且比环境里装的新，就把包的 ``__path__`` 指过去，之后导入的 ``jianshanagent.*`` 都来自更新包。
环境里装的版本追上来以后
（重建了镜像、``uv sync --upgrade-package``），更新包自动不再用。

刚装上、还没确认过的更新包只有机器人进程（:func:`launch`）会去试：试的时候起不来（抛了异常，或者
连着几次没走到「启动完成」），就记成失败、退回原来的版本重新启动。别的进程（知识库自动更新、命令行）
只用确认过的。

**这个模块永远是环境里装的那一份**（它在 ``__path__`` 换掉之前就导入了），更新包换不掉它。所以：

- 只用标准库，不导入包里别的模块；任何一步出错都当作「没有更新包」，不能让它拦住启动；
- 目录的样子和 ``state.json`` 的格式要一直兼容。要改就加新的格式号（``FORMAT``），旧的引导不认识
  新格式时会当作没有更新包，照环境里装的版本运行；
- 更新包里的代码只能用这里已经有的函数。往这里加了新函数，更新包要用 ``getattr`` 判断有没有。

不用更新包的情况（:func:`mode`）：从源码运行（可编辑安装，或者根本没装成包）——更新包会盖住正在改的源码；
按发布流程部署的（环境变量 ``JIANSHANAGENT_RELEASE``）——用哪个版本由发布说了算。
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sys
import time
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path
from typing import Any

PACKAGE = "jianshanagent"
#: state.json 的格式号
FORMAT = 1
STATE_FILE = "state.json"
VERSIONS_DIR = "versions"
#: 更新目录在哪（不设就是实例目录的 data/core）
UPDATE_DIR_ENV = "JIANSHANAGENT_UPDATE_DIR"
CONFIG_DIR_ENV = "JIANSHANAGENT_CONFIG_DIR"
#: 按发布流程部署时带着的发布号：有它就不用更新包
RELEASE_ENV = "JIANSHANAGENT_RELEASE"
#: 刚装上的更新包最多试几次启动；还没走到「启动完成」就算它起不来
MAX_ATTEMPTS = 2

MODE_OVERLAY = "overlay"  # 可以用更新包
MODE_MANAGED = "managed"  # 按发布流程部署的
MODE_SOURCE = "source"  # 从源码运行
MODE_NONE = "none"  # 找不到实例目录，没有地方放更新包

_VERSION = re.compile(r"^v?(\d+(?:\.\d+)*)$")

#: 现在用的更新包版本（空 = 用环境里装的）
_active = ""
#: 这个进程正在试一个还没确认的更新包
_trial = False


def base_version() -> str:
    """环境里装的版本；没装成包（直接从源码目录运行）时是 ``0.0.0.dev``。"""
    try:
        return distribution(PACKAGE).version
    except PackageNotFoundError:
        return "0.0.0.dev"


def running_version() -> str:
    """这个进程实际在用的版本。"""
    return _active or base_version()


def parse_version(text: str) -> tuple[int, ...] | None:
    """``0.3.1`` / ``v0.3.1`` → ``(0, 3, 1)``。

    只认纯数字的正式版本号，别的（开发版、预发布）是 None。
    """
    match = _VERSION.match(str(text).strip())
    return tuple(int(part) for part in match.group(1).split(".")) if match else None


def newer(candidate: str, than: str) -> bool:
    """``candidate`` 是不是比 ``than`` 新。有一边认不出来就不算新（宁可不更新）。"""
    left, right = parse_version(candidate), parse_version(than)
    if left is None or right is None:
        return False
    width = max(len(left), len(right))
    return left + (0,) * (width - len(left)) > right + (0,) * (width - len(right))


def update_dir() -> Path | None:
    """更新目录。找不到实例目录（不在实例目录里运行，也没设环境变量）时是 None。"""
    explicit = os.environ.get(UPDATE_DIR_ENV, "").strip()
    if explicit:
        return Path(explicit).expanduser()
    config = os.environ.get(CONFIG_DIR_ENV, "").strip()
    if config:
        return Path(config).expanduser().parent / "data" / "core"
    here = Path.cwd()
    return here / "data" / "core" if (here / "config").is_dir() else None


def _editable() -> bool:
    """是不是从源码运行：没装成包，或者是可编辑安装。"""
    try:
        raw = distribution(PACKAGE).read_text("direct_url.json")
    except PackageNotFoundError:
        return True
    if not raw:
        return False
    try:
        return bool((json.loads(raw).get("dir_info") or {}).get("editable"))
    except (ValueError, AttributeError):
        return False


def mode() -> tuple[str, str]:
    """这个环境能不能用更新包 →（哪一种，给人看的原因）。"""
    if os.environ.get(RELEASE_ENV, "").strip():
        return MODE_MANAGED, "这个部署是按发布流程上线的，用哪个版本由发布决定"
    if _editable():
        return MODE_SOURCE, "核心是从源码运行的（可编辑安装），用 git 更新"
    if update_dir() is None:
        return MODE_NONE, "不在实例目录里运行，没有地方放更新包"
    return MODE_OVERLAY, ""


def read_state(directory: Path) -> dict[str, Any]:
    """读 state.json。没有、读不了、格式不认识，都是空的。

    ::

        {"format": 1,
         "current": "0.3.1",         现在该用哪个更新包；空 = 不用
         "status": "pending" | "ok",  刚装上还没确认 / 确认过能起来
         "attempts": 1,               pending 时已经试了几次启动
         "previous": "0.3.0",         上一个确认过的更新包（current 起不来时退回它）；可以没有
         "applied_at": 1760000000.0,
         "last_failure": {"version": "0.3.1", "error": "...", "at": 1760000000.0}}
    """
    try:
        data = json.loads((directory / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) and data.get("format") == FORMAT else {}


def write_state(directory: Path, state: dict[str, Any]) -> None:
    """写 state.json：先写到旁边再改名，写到一半断电不会留下半个文件。"""
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / f".{STATE_FILE}.tmp"
    temporary.write_text(
        json.dumps({**state, "format": FORMAT}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(directory / STATE_FILE)


def package_dir(directory: Path, version: str) -> Path:
    """某个版本的更新包里，``jianshanagent`` 包的目录。"""
    return directory / VERSIONS_DIR / version / PACKAGE


def usable(directory: Path, version: str) -> bool:
    """这个版本的更新包能不能用：版本号认得、比环境里装的新、包在。"""
    return bool(
        version
        and parse_version(version) is not None
        and newer(version, base_version())
        and (package_dir(directory, version) / "__init__.py").is_file()
    )


def _switch(directory: Path, version: str) -> None:
    """把 ``jianshanagent`` 包指到更新包上。要在导入包里别的模块之前做。"""
    global _active
    package = sys.modules[PACKAGE]
    target = str(package_dir(directory, version))
    package.__path__[:] = [target]
    spec = getattr(package, "__spec__", None)
    locations = getattr(spec, "submodule_search_locations", None)
    if locations is not None and locations is not package.__path__:
        locations[:] = [target]
    _active = version
    # __init__.py 是环境里装的那一份，它里面的 __version__ 也要跟着换
    package.__version__ = version  # type: ignore[attr-defined]


def confirmed_version(directory: Path, state: dict[str, Any] | None = None) -> str:
    """确认过能起来、现在该用的更新包版本；没有就是空的。

    ``current`` 还在试（pending）时，是它前面那一个。
    """
    state = read_state(directory) if state is None else state
    key = "current" if state.get("status") == "ok" else "previous"
    version = str(state.get(key) or "")
    return version if usable(directory, version) else ""


def activate() -> None:
    """导入 ``jianshanagent`` 时调：有确认过的更新包就用它。出什么错都不拦启动。"""
    try:
        if mode()[0] != MODE_OVERLAY:
            return
        directory = update_dir()
        if directory is None:
            return
        version = confirmed_version(directory)
        if version:
            _switch(directory, version)
    except Exception as exc:  # 引导不能把进程弄死：当作没有更新包
        print(f"[{PACKAGE}] 没用上更新包：{type(exc).__name__}: {exc}", file=sys.stderr)


def _give_up(directory: Path, state: dict[str, Any], reason: str) -> None:
    """``current`` 那个更新包起不来：记下来，退回上一个确认过的（没有就退回环境里装的）。"""
    failed = str(state.get("current") or "")
    previous = str(state.get("previous") or "")
    back = previous if usable(directory, previous) else ""
    write_state(
        directory,
        {
            "current": back,
            "status": "ok" if back else "",
            "attempts": 0,
            "previous": "",
            "applied_at": state.get("applied_at"),
            "last_failure": {"version": failed, "error": reason[:2000], "at": time.time()},
        },
    )
    print(
        f"[{PACKAGE}] 更新包 {failed} 没有起来（{reason[:300]}），"
        f"退回 {back or '环境里装的 ' + base_version()}",
        file=sys.stderr,
    )


def _begin_trial() -> None:
    """有刚装上、还没确认的更新包就试它：记一次尝试，把包指过去。"""
    global _trial
    if mode()[0] != MODE_OVERLAY:
        return
    directory = update_dir()
    if directory is None:
        return
    state = read_state(directory)
    if state.get("status") != "pending":
        return
    version = str(state.get("current") or "")
    attempts = int(state.get("attempts") or 0) + 1
    if not usable(directory, version):
        _give_up(directory, state, "更新包不在了，或者不比环境里装的版本新")
        return
    if attempts > MAX_ATTEMPTS:
        _give_up(directory, state, f"试了 {MAX_ATTEMPTS} 次都没有走到启动完成")
        return
    write_state(directory, {**state, "attempts": attempts})
    # 导入时可能已经指到了上一个确认过的更新包；这时包里别的模块都还没导入，换过去没有问题
    _switch(directory, version)
    _trial = True


def confirm() -> bool:
    """机器人启动完成了：正在试的更新包记成确认过的。返回这次是不是确认了一个更新包。"""
    global _trial
    if not _trial:
        return False
    directory = update_dir()
    if directory is None:
        return False
    state = read_state(directory)
    if state.get("current") == _active:
        confirmed = {**state, "status": "ok", "attempts": 0, "confirmed_at": time.time()}
        write_state(directory, confirmed)
    _trial = False
    return True


def trial() -> bool:
    """这个进程是不是正在试一个还没确认的更新包。"""
    return _trial


def launch() -> int:
    """机器人的入口（命令 ``jianshanagent`` 和 ``python -m jianshanagent`` 都走这里）。

    有刚装上的更新包就先试它；试的时候启动失败，退回原来的版本重新启动一次。
    """
    with contextlib.suppress(Exception):  # 引导出错不拦启动：照环境里装的版本运行
        _begin_trial()
    try:
        from jianshanagent.main import main

        return main()
    except Exception as exc:
        if not _trial:
            raise
        directory = update_dir()
        if directory is None:
            raise
        _give_up(directory, read_state(directory), f"{type(exc).__name__}: {exc}")
        sys.stderr.flush()
        os.execv(sys.executable, [sys.executable, "-m", PACKAGE, *sys.argv[1:]])
