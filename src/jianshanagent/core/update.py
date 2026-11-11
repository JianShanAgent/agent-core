"""核心的更新：查主仓库有没有新版本，把新版本装成「更新包」。

主仓库每发一个版本（GitHub Release，标签 ``v0.3.1``），带一个安装包
``jianshanagent-0.3.1-py3-none-any.whl``。这里做三件事：

- **查**（:meth:`Updater.check`）：问 GitHub 最新的 Release 是哪个版本，比正在用的新就在
  管理后台里提示。机器人启动后过一会儿查一次，以后每隔几小时查一次；查不成只记下原因，不影响别的。
- **装**（:meth:`Updater.apply`）：下载安装包，核对校验和，检查它能不能用在这个环境里，
  解到更新目录的 ``versions/<版本>/``，在 ``state.json`` 里记成「待确认」。
  然后由调用的一方重启机器人。
- **确认**（:meth:`Updater.started`）：机器人启动完成时调。正在试的更新包记成「确认过」，
  顺手清掉用不着的旧版本。

更新包怎么盖住环境里装的版本、起不来怎么退回，在 ``_bootstrap.py``（它是更新包换不掉的那一小块）。

什么时候不让装（:meth:`Updater.status` 的 ``blocked``）：

- 不是能用更新包的环境（从源码运行、按发布流程部署的），见 ``_bootstrap.mode``；
- 新版本声明的第三方依赖和环境里装的那一份不一样：更新包只换核心自己的代码，依赖要重建环境才有；
- 新版本要的 Python 比现在的新；这个 Release 没带安装包，或者没有校验和。

安装包只认纯 Python 的（``py3-none-any``），里面只能有 ``jianshanagent/`` 和它的 ``.dist-info/``。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import re
import shutil
import sys
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from email.parser import Parser
from importlib import metadata
from pathlib import Path, PurePosixPath
from typing import Any

import httpx

from jianshanagent import _bootstrap
from jianshanagent.core.config import AppConfig
from jianshanagent.core.logging import get_logger

logger = get_logger(__name__)

CONFIG_NAME = "core.update"
PACKAGE = _bootstrap.PACKAGE
#: 安装包最多这么大（现在约 1 MB，大头是管理后台的页面）
MAX_WHEEL_BYTES = 64 * 1024 * 1024
#: 校验和清单这个文件最多读这么多
MAX_SUMS_BYTES = 64 * 1024
#: Release 里校验和清单的文件名（每行「sha256  文件名」）
SUMS_FILE = "SHA256SUMS"
#: 启动后过多久查第一次
FIRST_CHECK_SECONDS = 20.0
_TMP_DIR = "tmp"


class UpdateError(Exception):
    """更新做不了。信息是给管理后台的人看的。"""


@dataclass(frozen=True, slots=True)
class UpdateSettings:
    enabled: bool = True
    repo: str = "JianShanAgent/agent-core"
    api_url: str = "https://api.github.com"
    download_prefix: str = ""
    check_interval_hours: float = 6.0
    timeout_seconds: float = 15.0

    @classmethod
    def from_config(cls, config: AppConfig) -> UpdateSettings:
        raw = config.get(CONFIG_NAME)
        defaults = cls()
        repo = str(raw.get("repo") or defaults.repo).strip().strip("/")
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
            raise ValueError(f"core.update.yaml 的 repo 要写成 <组织>/<仓库>：{repo}")
        return cls(
            enabled=bool(raw.get("enabled", defaults.enabled)),
            repo=repo,
            api_url=str(raw.get("api_url") or defaults.api_url).rstrip("/"),
            download_prefix=str(raw.get("download_prefix") or ""),
            check_interval_hours=max(
                1.0, float(raw.get("check_interval_hours") or defaults.check_interval_hours)
            ),
            timeout_seconds=max(1.0, float(raw.get("timeout_seconds") or defaults.timeout_seconds)),
        )


@dataclass(frozen=True, slots=True)
class Release:
    """主仓库的一个版本。"""

    version: str
    tag: str
    name: str
    notes: str
    published_at: str
    url: str
    #: 安装包（没带就是空的）
    asset_name: str = ""
    asset_url: str = ""
    asset_size: int = 0
    #: 安装包的 sha256；Release 的接口里没给时是空的，装的时候再去校验和清单里找
    sha256: str = ""
    #: 校验和清单的下载地址
    sums_url: str = ""

    def brief(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "tag": self.tag,
            "name": self.name,
            "notes": self.notes,
            "published_at": self.published_at,
            "url": self.url,
            "size": self.asset_size,
            "has_package": bool(self.asset_url),
        }


def wheel_name(version: str) -> str:
    return f"{PACKAGE}-{version}-py3-none-any.whl"


def parse_release(data: dict[str, Any]) -> Release:
    """GitHub 接口返回的一个 Release → :class:`Release`。标签认不出版本号就报错。"""
    tag = str(data.get("tag_name") or "")
    parsed = _bootstrap.parse_version(tag)
    if parsed is None:
        raise UpdateError(f"最新的 Release 的标签不是版本号：{tag or '（空）'}")
    version = ".".join(str(part) for part in parsed)
    assets = [item for item in data.get("assets") or [] if isinstance(item, dict)]
    by_name = {str(item.get("name") or ""): item for item in assets}
    wheel = by_name.get(wheel_name(version)) or {}
    digest = str(wheel.get("digest") or "")
    return Release(
        version=version,
        tag=tag,
        name=str(data.get("name") or tag),
        notes=str(data.get("body") or "").strip(),
        published_at=str(data.get("published_at") or ""),
        url=str(data.get("html_url") or ""),
        asset_name=str(wheel.get("name") or ""),
        asset_url=str(wheel.get("browser_download_url") or ""),
        asset_size=int(wheel.get("size") or 0),
        sha256=digest.removeprefix("sha256:").lower() if digest.startswith("sha256:") else "",
        sums_url=str((by_name.get(SUMS_FILE) or {}).get("browser_download_url") or ""),
    )


def _normalize(requirement: str) -> str:
    return re.sub(r"\s+", "", requirement).lower()


def _core_requirements(lines: list[str] | None) -> set[str]:
    """依赖声明里不带 extra 的那些（可选依赖不算）。"""
    return {_normalize(line) for line in lines or [] if "extra==" not in _normalize(line)}


def _python_ok(spec: str) -> bool:
    """``Requires-Python`` 现在的 Python 满不满足。只认 ``>=X.Y`` 这种写法，别的写法不拦。"""
    for part in spec.split(","):
        match = re.fullmatch(r"\s*>=\s*(\d+)\.(\d+)(?:\.\d+)?\s*", part)
        if match and sys.version_info[:2] < (int(match.group(1)), int(match.group(2))):
            return False
    return True


def install_wheel(wheel: Path, version: str, directory: Path) -> Path:
    """检查安装包，解到 ``directory/versions/<version>/``，返回那个目录。

    不合适就报 :class:`UpdateError`。

    检查的是：它是不是 ``jianshanagent`` 的那个版本、纯 Python、里面只有包自己的文件；
    要的 Python 现在满足不满足；声明的依赖是不是环境里装的那一份都有；每个 ``.py`` 能不能编译。
    """
    info_dir = f"{PACKAGE}-{version}.dist-info"
    try:
        archive = zipfile.ZipFile(wheel)
    except (zipfile.BadZipFile, OSError) as exc:
        raise UpdateError(f"安装包打不开：{exc}") from exc
    with archive:
        names = archive.namelist()
        for name in names:
            path = PurePosixPath(name)
            if path.is_absolute() or ".." in path.parts or path.parts[0] not in {PACKAGE, info_dir}:
                raise UpdateError(f"安装包里有不该有的文件：{name}")
        try:
            meta = Parser().parsestr(archive.read(f"{info_dir}/METADATA").decode("utf-8"))
            tags = Parser().parsestr(archive.read(f"{info_dir}/WHEEL").decode("utf-8"))
        except KeyError as exc:
            raise UpdateError(f"安装包里没有 {info_dir} 的元数据") from exc
        if _normalize(meta.get("Name", "")) != PACKAGE or meta.get("Version", "") != version:
            raise UpdateError(
                f"安装包不是 {PACKAGE} {version}，而是 {meta.get('Name')} {meta.get('Version')}"
            )
        if "py3-none-any" not in (tags.get_all("Tag") or []):
            raise UpdateError("安装包不是纯 Python 的，不能当更新包用")
        if f"{PACKAGE}/__init__.py" not in names:
            raise UpdateError("安装包里没有 jianshanagent 包")
        python = meta.get("Requires-Python", "")
        if python and not _python_ok(python):
            raise UpdateError(
                f"新版本要 Python {python}，现在是 {sys.version.split()[0]}：要重建环境才能更新"
            )
        try:
            have = _core_requirements(metadata.requires(PACKAGE))
        except metadata.PackageNotFoundError:
            have = set()
        missing = sorted(_core_requirements(meta.get_all("Requires-Dist")) - have)
        if missing:
            raise UpdateError(
                "新版本的第三方依赖和环境里装的不一样（" + "、".join(missing) + "），"
                "在程序里更新不了，要重建环境：Docker 方式在实例目录里 "
                "`docker compose build --no-cache && docker compose up -d`；"
                "uv 方式 `uv sync --upgrade-package jianshanagent` 后重启"
            )
        target = directory / _bootstrap.VERSIONS_DIR / version
        staging = target.with_name(f"{version}.tmp")
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        try:
            archive.extractall(staging)
            for source in sorted((staging / PACKAGE).rglob("*.py")):
                try:
                    compile(source.read_bytes(), str(source), "exec")
                except SyntaxError as exc:
                    where = source.relative_to(staging)
                    raise UpdateError(f"安装包里的 {where} 编译不过：{exc.msg}") from exc
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
    shutil.rmtree(target, ignore_errors=True)
    staging.rename(target)
    return target


class Updater:
    """查新版本、装更新包。一个进程一个，挂在 ``Runtime`` 上。"""

    def __init__(
        self,
        settings: Callable[[], UpdateSettings],
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._settings = settings
        self._transport = transport
        #: 上一次查到的最新版本；还没查到过（或者主仓库还没发过版本）是 None
        self.latest: Release | None = None
        self.checked_at: float | None = None
        #: 上一次查没查成的原因；查成了是空的
        self.error = ""
        self.applying = False
        self._lock = asyncio.Lock()

    # ---- 现状 ----

    @property
    def current(self) -> str:
        return _bootstrap.running_version()

    @property
    def available(self) -> bool:
        """有没有比正在用的新的版本。"""
        return self.latest is not None and _bootstrap.newer(self.latest.version, self.current)

    def _state(self) -> dict[str, Any]:
        directory = _bootstrap.update_dir()
        return _bootstrap.read_state(directory) if directory is not None else {}

    def blocked(self) -> str:
        """现在不能在程序里装新版本的原因；能装就是空的。"""
        kind, reason = _bootstrap.mode()
        if kind != _bootstrap.MODE_OVERLAY:
            return reason
        if self.latest is None or not self.available:
            return "没有比现在更新的版本"
        if not self.latest.asset_url:
            return f"{self.latest.tag} 这个版本没有带安装包（{wheel_name(self.latest.version)}）"
        if not self.latest.sha256 and not self.latest.sums_url:
            return f"{self.latest.tag} 这个版本没有带校验和，不装来路核对不了的包"
        return ""

    def brief(self) -> dict[str, Any]:
        """给侧栏用的一小份：有没有新版本、是哪个。"""
        return {"available": self.available, "latest": self.latest.version if self.latest else None}

    def status(self) -> dict[str, Any]:
        try:
            settings = self._settings()
            enabled, repo = settings.enabled, settings.repo
        except ValueError as exc:
            enabled, repo = False, ""
            self.error = str(exc)
        kind, reason = _bootstrap.mode()
        state = self._state()
        base = _bootstrap.base_version()
        blocked = self.blocked()
        return {
            "enabled": enabled,
            "repo": repo,
            #: 正在用的版本；环境里装的版本（用着更新包时两个不一样）
            "current": self.current,
            "installed": base,
            "overlay": self.current != base,
            #: 这个进程正在试一个刚装上的更新包（启动完成前）
            "trial": _bootstrap.trial(),
            "mode": kind,
            "mode_reason": reason,
            "latest": self.latest.brief() if self.latest else None,
            "available": self.available,
            "checked_at": self.checked_at,
            "error": self.error,
            "can_apply": self.available and not blocked,
            "blocked": blocked if self.available else "",
            "applying": self.applying,
            "last_failure": state.get("last_failure") or None,
        }

    # ---- 查 ----

    def _client(self, settings: UpdateSettings) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=settings.timeout_seconds,
            follow_redirects=True,
            transport=self._transport,
            headers={"User-Agent": f"JianShanAgent/{self.current} (update check)"},
        )

    async def check(self) -> Release | None:
        """问主仓库最新的版本。查不成不抛异常：原因记在 :attr:`error` 里，上一次查到的留着。"""
        try:
            settings = self._settings()
            url = f"{settings.api_url}/repos/{settings.repo}/releases/latest"
            async with self._client(settings) as client:
                response = await client.get(url, headers={"Accept": "application/vnd.github+json"})
            if response.status_code == 404:
                # 仓库在、还没发过版本
                self.latest, self.error = None, ""
            else:
                response.raise_for_status()
                self.latest, self.error = parse_release(response.json()), ""
        except (httpx.HTTPError, ValueError, UpdateError) as exc:
            self.error = f"{type(exc).__name__}: {exc}"[:500]
            logger.warning("update_check_failed", error=self.error)
        self.checked_at = time.time()
        if self.available and self.latest is not None:
            logger.info("update_available", current=self.current, latest=self.latest.version)
        return self.latest

    async def run_forever(self) -> None:
        """定时查。配置里关掉了就空转（下一轮再看配置，后台里打开了不用重启）。"""
        await asyncio.sleep(FIRST_CHECK_SECONDS)
        while True:
            hours = UpdateSettings().check_interval_hours
            try:
                settings = self._settings()
                hours = settings.check_interval_hours
                if settings.enabled:
                    await self.check()
            except Exception:
                logger.exception("update_loop_failed")
            await asyncio.sleep(hours * 3600)

    # ---- 装 ----

    async def _download(self, client: httpx.AsyncClient, url: str, target: Path) -> str:
        """下到 ``target``，返回它的 sha256。超过大小上限就停。"""
        digest = hashlib.sha256()
        size = 0
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            with target.open("wb") as handle:
                async for piece in response.aiter_bytes():
                    size += len(piece)
                    if size > MAX_WHEEL_BYTES:
                        raise UpdateError("安装包大得不正常，没有装")
                    digest.update(piece)
                    handle.write(piece)
        return digest.hexdigest()

    async def _expected_sha256(self, client: httpx.AsyncClient, release: Release) -> str:
        if release.sha256:
            return release.sha256
        response = await client.get(release.sums_url)
        response.raise_for_status()
        for line in response.text[:MAX_SUMS_BYTES].splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1].lstrip("*") == release.asset_name:
                return parts[0].lower()
        raise UpdateError(f"校验和清单里没有 {release.asset_name}")

    async def apply(self) -> str:
        """把最新的版本装成更新包，记成待确认。返回装的版本；之后要重启机器人才用上。"""
        if self._lock.locked():
            raise UpdateError("正在更新，等它做完")
        async with self._lock:
            settings = self._settings()
            await self.check()  # 装之前再问一次：页面上看到的可能是几小时前的
            reason = self.blocked()
            if reason:
                raise UpdateError(reason)
            release = self.latest
            directory = _bootstrap.update_dir()
            assert release is not None and directory is not None
            self.applying = True
            tmp = directory / _TMP_DIR
            wheel = tmp / release.asset_name
            try:
                tmp.mkdir(parents=True, exist_ok=True)
                async with self._client(settings) as client:
                    expected = await self._expected_sha256(client, release)
                    actual = await self._download(
                        client, settings.download_prefix + release.asset_url, wheel
                    )
                if actual != expected:
                    raise UpdateError(
                        "下载到的安装包校验和对不上"
                        f"（应该是 {expected[:12]}…，实际是 {actual[:12]}…），没有装"
                    )
                await asyncio.to_thread(install_wheel, wheel, release.version, directory)
                state = _bootstrap.read_state(directory)
                _bootstrap.write_state(
                    directory,
                    {
                        "current": release.version,
                        "status": "pending",
                        "attempts": 0,
                        "previous": _bootstrap.confirmed_version(directory, state),
                        "applied_at": time.time(),
                        "last_failure": None,
                    },
                )
            except httpx.HTTPError as exc:
                raise UpdateError(f"下载没成：{type(exc).__name__}: {exc}"[:500]) from exc
            except OSError as exc:
                raise UpdateError(f"更新目录写不了（{directory}）：{exc}") from exc
            finally:
                self.applying = False
                shutil.rmtree(tmp, ignore_errors=True)
            logger.warning("update_installed", version=release.version, previous=self.current)
            return release.version

    def revert(self) -> str:
        """不用更新包了，回到环境里装的版本。返回那个版本；之后要重启机器人。"""
        kind, reason = _bootstrap.mode()
        directory = _bootstrap.update_dir()
        if kind != _bootstrap.MODE_OVERLAY or directory is None:
            raise UpdateError(reason or "这个环境没有用更新包")
        if not _bootstrap.read_state(directory).get("current"):
            raise UpdateError("现在用的就是环境里装的版本")
        _bootstrap.write_state(directory, {"current": "", "status": "", "last_failure": None})
        logger.warning("update_reverted", to=_bootstrap.base_version())
        return _bootstrap.base_version()

    # ---- 确认 ----

    def started(self) -> None:
        """机器人启动完成了：确认正在试的更新包，清掉用不着的旧版本。出错只记日志。"""
        try:
            if _bootstrap.confirm():
                logger.warning("update_confirmed", version=self.current)
            if _bootstrap.mode()[0] != _bootstrap.MODE_OVERLAY:
                return
            directory = _bootstrap.update_dir()
            versions = directory / _bootstrap.VERSIONS_DIR if directory is not None else None
            if directory is None or versions is None or not versions.is_dir():
                return
            state = _bootstrap.read_state(directory)
            keep = {str(state.get("current") or ""), str(state.get("previous") or "")}
            # 环境里装的版本追上来以后，更新包就用不着了
            keep = {name for name in keep if _bootstrap.usable(directory, name)}
            for child in versions.iterdir():
                if child.name not in keep:
                    shutil.rmtree(child, ignore_errors=True)
            if state.get("current") and not keep:
                _bootstrap.write_state(
                    directory,
                    {"current": "", "status": "", "last_failure": state.get("last_failure")},
                )
            with contextlib.suppress(OSError):
                shutil.rmtree(directory / _TMP_DIR)
        except Exception:
            logger.exception("update_housekeeping_failed")


def restart_needed() -> bool:
    """长时间运行的别的进程（知识库自动更新）要不要原地重启一下：确认过的更新包换了，自己还在用旧的。"""
    if _bootstrap.mode()[0] != _bootstrap.MODE_OVERLAY:
        return False
    directory = _bootstrap.update_dir()
    if directory is None:
        return False
    wanted = _bootstrap.confirmed_version(directory) or _bootstrap.base_version()
    return wanted != _bootstrap.running_version()
