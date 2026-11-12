"""站点目录：做好了的站点插件有哪些；把其中一个下载下来。

目录是 JianShanAgent 组织里的一个文件（``catalog`` 仓库的 ``sites.yaml``），管理后台的「Wiki」页
从这里列出来、按平台筛选，点一下就把那个 wiki 挂上（接口在 ``admin/routes_wikis.py``）::

    format: 1
    sites:
      - id: huiji_yys                      # 站点插件的 id
        platform: huiji                    # 它是哪个平台上的站；自己架的 MediaWiki 站不写
        name: 阴阳师 wiki                   # 给人看的名字
        description: …                     # 一两句话说明它带来什么
        repo: JianShanAgent/site-huiji-yys # 站点插件的仓库：从它的 Release 下载
        wiki:                              # 挂上这个 wiki 时写进 core.wiki.yaml 的那一项
          id: huiji                        # 建议的名字（添加时可以改）
          label: 灰机 wiki
          api_url: https://yys.huijiwiki.com/api.php
          site_base: …                     # 这三项不写就按 MediaWiki 的默认
          article_path: …
          auth_header: X-authkey           # 要带放行请求头的站：请求头的名字（密钥在 secrets.yaml）

这里做三件事：

- **读**（:meth:`SiteCatalog.load`）：经 GitHub 的接口读那个文件，在内存里留几分钟。
  读不成、但之前读到过，就先用之前的，原因记在 :attr:`SiteCatalog.error` 里。
- **下载**（:meth:`SiteCatalog.install`）：从站点插件的仓库最新的 Release 取安装包
  （``jianshanagent_site_<id>-<版本>-py3-none-any.whl``），核对校验和，检查它用不用得到这个环境里，
  解到下载目录的 ``<id>/<版本>/``，在 ``state.json`` 里记下来。之后 ``core/manifest.py``
  就找得到它（:func:`~jianshanagent.core.manifest.installed_sites`），不用重启。
- **删**（:func:`uninstall`）：没有 wiki 用它了，把下载的那一份删掉。

守着的几条：

- 目录在哪（``core.catalog.yaml`` 的 ``repo``、``path``、``ref``）不能在网页上改；
  目录里的站点插件只认和目录同一个组织下的仓库。
- 没有校验和的包不装；下载到的和校验和对不上不装。
- 安装包只认纯 Python 的，里面只能有那个站点插件的包和它的 ``.dist-info/``；它声明的依赖要
  环境里都有、版本够（它要的核心比现在的新，就提示先更新核心）；每个 ``.py`` 要能编译；
  清单要合规范、写的平台和目录里说的一样。
- 环境里已经装着的站点插件（随部署带着的）不下载、也不会被下载的盖住。
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import sys
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from email.parser import Parser
from importlib import metadata
from pathlib import Path, PurePosixPath
from typing import Any

import httpx
import yaml

from jianshanagent import _bootstrap
from jianshanagent.core.config import AppConfig
from jianshanagent.core.logging import get_logger
from jianshanagent.core.manifest import (
    DEFAULT_PLATFORM,
    KIND_SITE,
    SITES_STATE_FILE,
    SITES_STATE_FORMAT,
    PluginError,
    clear_caches,
    downloaded_site_dir,
    platforms,
    read_manifest,
    read_sites_state,
    site_package,
)
from jianshanagent.core.update import (
    PACKAGE,
    Release,
    UpdateError,
    _python_ok,
    download,
    expected_sha256,
    parse_release,
    wheel_name,
)

logger = get_logger(__name__)

CONFIG_NAME = "core.catalog"
CATALOG_FORMAT = 1
#: 目录文件最多读这么多
MAX_CATALOG_BYTES = 1024 * 1024
_TMP_DIR = ".tmp"
_SITE_ID = re.compile(r"[a-z][a-z0-9_]*")
_WIKI_ID = _SITE_ID
_REPO = re.compile(r"[\w.-]+/[\w.-]+")
#: 目录里一个 wiki 可以写的项（都会原样写进 core.wiki.yaml 的那一项）
_WIKI_KEYS = ("api_url", "site_base", "article_path", "auth_header")
#: 依赖声明：名字、可选的 extras、版本要求、可选的环境标记
_REQUIREMENT = re.compile(
    r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*\(?([^;()]*)\)?\s*(?:;(.*))?"
)
_SPECIFIER = re.compile(r"\s*(>=|==|>)\s*v?(\d+(?:\.\d+)*)\s*")


class CatalogError(Exception):
    """站点目录读不了、站点插件装不上。信息是给管理后台的人看的。"""


@dataclass(frozen=True, slots=True)
class CatalogSettings:
    enabled: bool = True
    repo: str = "JianShanAgent/catalog"
    path: str = "sites.yaml"
    ref: str = "main"
    api_url: str = "https://api.github.com"
    download_prefix: str = ""
    cache_minutes: float = 10.0
    timeout_seconds: float = 15.0

    @classmethod
    def from_config(cls, config: AppConfig) -> CatalogSettings:
        raw = config.get(CONFIG_NAME)
        defaults = cls()
        repo = str(raw.get("repo") or defaults.repo).strip().strip("/")
        if not _REPO.fullmatch(repo):
            raise ValueError(f"core.catalog.yaml 的 repo 要写成 <组织>/<仓库>：{repo}")
        return cls(
            enabled=bool(raw.get("enabled", defaults.enabled)),
            repo=repo,
            path=str(raw.get("path") or defaults.path).strip().strip("/"),
            ref=str(raw.get("ref") or defaults.ref).strip(),
            api_url=str(raw.get("api_url") or defaults.api_url).rstrip("/"),
            download_prefix=str(raw.get("download_prefix") or ""),
            cache_minutes=max(0.0, float(raw.get("cache_minutes", defaults.cache_minutes) or 0)),
            timeout_seconds=max(1.0, float(raw.get("timeout_seconds") or defaults.timeout_seconds)),
        )

    @property
    def owner(self) -> str:
        """目录所在的组织：目录里的站点插件只认这个组织下的仓库。"""
        return self.repo.split("/", 1)[0]


@dataclass(frozen=True, slots=True)
class CatalogSite:
    """目录里的一项：一个做好了的站点插件，和它那个 wiki 在哪。"""

    id: str
    platform: str
    name: str
    description: str
    #: 站点插件的仓库（``<组织>/<仓库>``）
    repo: str
    #: 建议的 wiki 名字、出处里的叫法
    wiki_id: str
    label: str
    #: 写进 ``core.wiki.yaml`` 那一项的地址和放行请求头的名字
    wiki: dict[str, str] = field(default_factory=dict)
    #: 这个版本的核心用不了它的原因（它在的平台核心还不认识）；能用就是空的
    unavailable: str = ""

    @property
    def package(self) -> str:
        return site_package(self.id)

    @property
    def auth_header(self) -> str:
        return self.wiki.get("auth_header", "")

    def wiki_entry(self, wiki_id: str, label: str) -> dict[str, Any]:
        """挂上它时写进 ``core.wiki.yaml`` 的那一项。地址只来自目录，不来自网页。"""
        entry: dict[str, Any] = {"id": wiki_id}
        if label:
            entry["label"] = label
        entry |= {key: self.wiki[key] for key in _WIKI_KEYS if self.wiki.get(key)}
        entry["site"] = self.id
        return entry


def _text(raw: dict[str, Any], key: str) -> str:
    return " ".join(str(raw.get(key) or "").split())


def _parse_site(raw: Any, owner: str) -> CatalogSite:
    """目录里的一项 → :class:`CatalogSite`；写得不对就报 :class:`CatalogError`。"""
    if not isinstance(raw, dict):
        raise CatalogError("有一项不是键值结构")
    site_id = str(raw.get("id") or "")
    if not _SITE_ID.fullmatch(site_id):
        raise CatalogError(f"有一项的 id 不对：「{site_id or '没写'}」")
    where = f"「{site_id}」"
    platform = str(raw.get("platform") or DEFAULT_PLATFORM)
    known = platforms()
    unavailable = ""
    if platform not in known:
        unavailable = f"它是「{platform}」平台上的站，这个版本的核心还不认识这个平台：先更新核心"
    elif platform != DEFAULT_PLATFORM and not site_id.startswith(f"{platform}_"):
        raise CatalogError(f"{where} 是「{platform}」平台上的站，id 却不是以 {platform}_ 开头")
    repo = str(raw.get("repo") or "").strip().strip("/")
    if not _REPO.fullmatch(repo):
        raise CatalogError(f"{where} 的 repo 要写成 <组织>/<仓库>")
    if repo.split("/", 1)[0].lower() != owner.lower():
        raise CatalogError(f"{where} 的仓库（{repo}）不在 {owner} 这个组织下")
    wiki = raw.get("wiki")
    if not isinstance(wiki, dict):
        raise CatalogError(f"{where} 没有写 wiki（它那个 wiki 在哪）")
    api_url = str(wiki.get("api_url") or "")
    if not api_url.startswith(("https://", "http://")):
        raise CatalogError(f"{where} 的 wiki.api_url 要是一个地址")
    wiki_id = str(wiki.get("id") or (platform if platform != DEFAULT_PLATFORM else site_id))
    if not _WIKI_ID.fullmatch(wiki_id):
        raise CatalogError(f"{where} 的 wiki.id 不对：「{wiki_id}」")
    name = _text(raw, "name")
    if not name:
        raise CatalogError(f"{where} 没有写 name")
    return CatalogSite(
        id=site_id,
        platform=platform,
        name=name,
        description=_text(raw, "description"),
        repo=repo,
        wiki_id=wiki_id,
        label=_text(wiki, "label"),
        wiki={key: str(wiki[key]).strip() for key in _WIKI_KEYS if wiki.get(key)},
        unavailable=unavailable,
    )


def parse_catalog(data: Any, *, owner: str) -> tuple[list[CatalogSite], list[str]]:
    """目录文件的内容 →（里面的站点插件，没用上的项和原因）。整个文件不对就报错。

    ``owner``：目录所在的组织。哪一项写得不对就跳过那一项，别的照常能用。
    """
    if not isinstance(data, dict) or not isinstance(data.get("sites"), list):
        raise CatalogError("站点目录的格式不对：要有 sites 这张名单")
    if data.get("format") != CATALOG_FORMAT:
        raise CatalogError(
            f"站点目录是第 {data.get('format')} 版格式，这个版本的核心只认第 {CATALOG_FORMAT} 版："
            "先更新核心"
        )
    sites: dict[str, CatalogSite] = {}
    problems: list[str] = []
    for raw in data["sites"]:
        try:
            site = _parse_site(raw, owner)
        except CatalogError as exc:
            problems.append(str(exc))
            continue
        if site.id in sites:
            problems.append(f"「{site.id}」写了两次")
            continue
        sites[site.id] = site
    return list(sites.values()), problems


# ---- 安装包 ----


def _dist_name(name: str) -> str:
    """发行包的名字规范化以后再比：``jianshanagent_site_x`` 和 ``jianshanagent-site-x`` 是一个。"""
    return re.sub(r"[-_.]+", "-", name).strip().lower()


def _installed_version(name: str) -> str | None:
    """环境里装的 ``name`` 是哪个版本；没装是 None。核心算正在用的那个版本（可能是更新包）。"""
    if _dist_name(name) == PACKAGE:
        return _bootstrap.running_version()
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def _satisfied(operator: str, have: tuple[int, ...], want: tuple[int, ...]) -> bool:
    width = max(len(have), len(want))
    left, right = have + (0,) * (width - len(have)), want + (0,) * (width - len(want))
    return {">=": left >= right, "==": left == right, ">": left > right}[operator]


def requirement_problem(requirement: str) -> str:
    """站点插件声明的一条依赖，这个环境满不满足；不满足返回原因，满足（或者是可选依赖）返回空的。

    下载的站点插件只带它自己的代码，依赖要环境里本来就有。版本只认 ``>=``、``==``、``>`` 和
    纯数字的版本号，别的写法不拦。
    """
    match = _REQUIREMENT.fullmatch(requirement)
    if match is None:
        return f"看不懂它声明的依赖：{requirement}"
    name, spec, marker = match.group(1), match.group(2) or "", match.group(3) or ""
    if "extra" in marker:
        return ""
    have = _installed_version(name)
    core = _dist_name(name) == PACKAGE
    if have is None:
        return f"它要用 {name}，环境里没有装：这个站点插件要装进环境里用（见它的 README）"
    current = _bootstrap.parse_version(have)
    for part in spec.split(","):
        found = _SPECIFIER.fullmatch(part)
        if found is None or current is None:
            continue
        wanted = tuple(int(piece) for piece in found.group(2).split("."))
        if not _satisfied(found.group(1), current, wanted):
            need = f"{found.group(1)}{found.group(2)}"
            if core:
                return f"它要核心 {need}，现在是 {have}：先更新核心（「系统信息」页）"
            return f"它要 {name}{need}，环境里是 {have}：这个站点插件要装进环境里用"
    return ""


def install_site_wheel(wheel: Path, site: CatalogSite, version: str, directory: Path) -> Path:
    """检查站点插件的安装包，解到 ``directory/<id>/<version>/``，返回那个目录。

    不合适就报 :class:`CatalogError`（检查些什么见本模块开头）。
    """
    package = site.package
    info_dir = f"{package}-{version}.dist-info"
    try:
        archive = zipfile.ZipFile(wheel)
    except (zipfile.BadZipFile, OSError) as exc:
        raise CatalogError(f"安装包打不开：{exc}") from exc
    target = downloaded_site_dir(directory, site.id, version)
    staging = target.with_name(f"{version}.tmp")
    with archive:
        names = archive.namelist()
        for name in names:
            path = PurePosixPath(name)
            if path.is_absolute() or ".." in path.parts or path.parts[0] not in {package, info_dir}:
                raise CatalogError(f"安装包里有不该有的文件：{name}")
        try:
            meta = Parser().parsestr(archive.read(f"{info_dir}/METADATA").decode("utf-8"))
            tags = Parser().parsestr(archive.read(f"{info_dir}/WHEEL").decode("utf-8"))
        except KeyError as exc:
            raise CatalogError(f"安装包里没有 {info_dir} 的元数据") from exc
        if _dist_name(meta.get("Name", "")) != _dist_name(package) or (
            meta.get("Version", "") != version
        ):
            raise CatalogError(
                f"安装包不是 {package} {version}，而是 {meta.get('Name')} {meta.get('Version')}"
            )
        if "py3-none-any" not in (tags.get_all("Tag") or []):
            raise CatalogError("安装包不是纯 Python 的，不能下载来用")
        if f"{package}/__init__.py" not in names:
            raise CatalogError(f"安装包里没有 {package} 这个包")
        python = meta.get("Requires-Python", "")
        if python and not _python_ok(python):
            raise CatalogError(f"它要 Python {python}，现在是 {sys.version.split()[0]}")
        for requirement in meta.get_all("Requires-Dist") or []:
            problem = requirement_problem(requirement)
            if problem:
                raise CatalogError(problem)
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        try:
            archive.extractall(staging)
            for source in sorted((staging / package).rglob("*.py")):
                try:
                    compile(source.read_bytes(), str(source), "exec")
                except SyntaxError as exc:
                    where = source.relative_to(staging)
                    raise CatalogError(f"安装包里的 {where} 编译不过：{exc.msg}") from exc
            try:
                manifest = read_manifest(staging / package, package, KIND_SITE, plugin_id=site.id)
            except PluginError as exc:
                raise CatalogError(f"它的清单不合这个版本的核心的规范：{exc}") from exc
            if manifest.platform != site.platform:
                raise CatalogError(
                    f"目录里说它是「{site.platform}」平台上的站，"
                    f"它的清单里写的却是「{manifest.platform}」"
                )
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
    shutil.rmtree(target, ignore_errors=True)
    staging.rename(target)
    return target


def write_sites_state(directory: Path, sites: dict[str, dict[str, Any]]) -> None:
    """记下「下载了哪些站点插件、各是哪个版本」。先写临时文件再改名。"""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / SITES_STATE_FILE
    temporary = path.with_name(f".{path.name}.tmp")
    content = {"format": SITES_STATE_FORMAT, "sites": sites}
    temporary.write_text(json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def uninstall(directory: Path, site_id: str) -> bool:
    """把下载的站点插件 ``site_id`` 删掉。返回删了没有（本来就没下载过是 False）。"""
    state = read_sites_state(directory)
    if site_id not in state:
        return False
    del state[site_id]
    write_sites_state(directory, state)
    shutil.rmtree(directory / site_id, ignore_errors=True)
    # 这个进程里导入过它的话也忘掉：以后再下载回来（多半是新的版本），用的才是新下载的代码
    package = site_package(site_id)
    for name in [n for n in sys.modules if n == package or n.startswith(f"{package}.")]:
        del sys.modules[name]
    home = str((directory / site_id).resolve())
    sys.path[:] = [entry for entry in sys.path if not entry.startswith(home)]
    clear_caches()
    logger.warning("site_plugin_removed", site=site_id)
    return True


class SiteCatalog:
    """读站点目录、下载站点插件。一个进程一个，挂在 ``Runtime`` 上。"""

    def __init__(
        self,
        settings: Callable[[], CatalogSettings],
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._settings = settings
        self._transport = transport
        self._sites: list[CatalogSite] | None = None
        self._fetched: float | None = None
        #: 上一次读到的目录是什么时候读的
        self.fetched_at: float | None = None
        #: 上一次读没读成的原因；读成了是空的
        self.error = ""
        #: 目录里没用上的项和原因
        self.problems: list[str] = []
        self._lock = asyncio.Lock()

    def settings(self) -> CatalogSettings:
        return self._settings()

    def _client(self, settings: CatalogSettings) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=settings.timeout_seconds,
            follow_redirects=True,
            transport=self._transport,
            headers={"User-Agent": f"JianShanAgent/{_bootstrap.running_version()} (site catalog)"},
        )

    async def load(self, *, refresh: bool = False) -> list[CatalogSite]:
        """目录里的站点插件。几分钟内读过就用读到的；``refresh`` = 现在就重读。

        读不成：之前读到过就返回之前的（原因在 :attr:`error` 里），从没读到过就报错。
        配置里关掉了返回空的。
        """
        settings = self._settings()
        if not settings.enabled:
            return []
        fresh = (
            self._sites is not None
            and self._fetched is not None
            and time.monotonic() - self._fetched < settings.cache_minutes * 60
        )
        if fresh and not refresh:
            return list(self._sites or [])
        url = f"{settings.api_url}/repos/{settings.repo}/contents/{settings.path}"
        try:
            async with self._client(settings) as client:
                response = await client.get(
                    url,
                    params={"ref": settings.ref},
                    headers={"Accept": "application/vnd.github.raw+json"},
                )
            if response.status_code == 404:
                raise CatalogError(f"没找到站点目录（{settings.repo} 的 {settings.path}）")
            response.raise_for_status()
            if len(response.content) > MAX_CATALOG_BYTES:
                raise CatalogError("站点目录大得不正常")
            sites, problems = parse_catalog(yaml.safe_load(response.text), owner=settings.owner)
        except (httpx.HTTPError, yaml.YAMLError, CatalogError) as exc:
            self.error = (
                str(exc) if isinstance(exc, CatalogError) else f"{type(exc).__name__}: {exc}"
            )[:500]
            logger.warning("site_catalog_failed", error=self.error)
            if self._sites is None:
                raise CatalogError(f"站点目录读不了：{self.error}") from exc
            return list(self._sites)
        self._sites, self.problems, self.error = sites, problems, ""
        self._fetched, self.fetched_at = time.monotonic(), time.time()
        if problems:
            logger.warning("site_catalog_entries_skipped", problems=problems)
        return list(sites)

    async def site(self, site_id: str) -> CatalogSite:
        """目录里叫 ``site_id`` 的那一项；没有就报错。"""
        for site in await self.load():
            if site.id == site_id:
                return site
        raise CatalogError(f"站点目录里没有「{site_id}」")

    async def latest(self, site: CatalogSite) -> Release:
        """站点插件的仓库最新的版本。还没发过版本、查不成都报错。"""
        settings = self._settings()
        url = f"{settings.api_url}/repos/{site.repo}/releases/latest"
        try:
            async with self._client(settings) as client:
                response = await client.get(url, headers={"Accept": "application/vnd.github+json"})
            if response.status_code == 404:
                raise CatalogError(f"{site.repo} 还没有发过版本，下载不了")
            response.raise_for_status()
            return parse_release(response.json(), site.package)
        except (httpx.HTTPError, ValueError, UpdateError) as exc:
            raise CatalogError(
                f"查 {site.repo} 的版本没成：{type(exc).__name__}: {exc}"[:500]
            ) from exc

    async def install(self, site: CatalogSite, directory: Path) -> str:
        """把 ``site`` 最新的版本下载到 ``directory``（下载目录），返回装的版本。

        已经是这个版本就什么都不做。装完 ``core/manifest.py`` 马上找得到它；
        它的代码已经被导入过的（更新），要重启才用上新的。
        """
        if site.unavailable:
            raise CatalogError(site.unavailable)
        async with self._lock:
            settings = self._settings()
            release = await self.latest(site)
            state = read_sites_state(directory)
            previous = str((state.get(site.id) or {}).get("version") or "")
            package_dir = downloaded_site_dir(directory, site.id, release.version) / site.package
            if previous == release.version and package_dir.is_dir():
                return release.version
            if not release.asset_url:
                raise CatalogError(
                    f"{site.repo} 的 {release.tag} 没有带安装包"
                    f"（{wheel_name(release.version, site.package)}）"
                )
            tmp = directory / _TMP_DIR
            wheel = tmp / release.asset_name
            try:
                tmp.mkdir(parents=True, exist_ok=True)
                async with self._client(settings) as client:
                    expected = await expected_sha256(client, release)
                    actual = await download(
                        client, settings.download_prefix + release.asset_url, wheel
                    )
                if actual != expected:
                    raise CatalogError(
                        "下载到的安装包校验和对不上"
                        f"（应该是 {expected[:12]}…，实际是 {actual[:12]}…），没有装"
                    )
                await asyncio.to_thread(install_site_wheel, wheel, site, release.version, directory)
                state[site.id] = {
                    "version": release.version,
                    "repo": site.repo,
                    "sha256": actual,
                    "installed_at": time.time(),
                }
                write_sites_state(directory, state)
            except UpdateError as exc:
                raise CatalogError(str(exc)) from exc
            except httpx.HTTPError as exc:
                raise CatalogError(f"下载没成：{type(exc).__name__}: {exc}"[:500]) from exc
            except OSError as exc:
                raise CatalogError(f"下载目录写不了（{directory}）：{exc}") from exc
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
            # 别的版本：上一个留着（正在跑的进程可能还要从它那里导入模块），更早的清掉
            for child in (directory / site.id).iterdir():
                if child.name not in {release.version, previous}:
                    shutil.rmtree(child, ignore_errors=True)
            clear_caches()
            logger.warning(
                "site_plugin_installed", site=site.id, version=release.version, previous=previous
            )
            return release.version
