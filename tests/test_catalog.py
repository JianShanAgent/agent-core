"""站点目录、站点插件的下载，和管理后台里添加、移除 wiki（``core/catalog.py``、
``admin/routes_wikis.py``，以及 ``core/config.py`` 里「后台加的 wiki」那一段）。

- 目录：只认同一个组织下的仓库；哪一项写坏了跳过那一项；核心还不认识的平台标出来。
- 下载：校验和对不上、带了不该有的文件、要更新的核心、清单和目录说的不一样，都不装；
  装上了不用重启就找得到，环境里已经有的不会被下载的盖住。
- 后台加的 wiki 接在部署写的后面；挂不上的跳过、不拦启动；改别的配置不动它们。
- 接口：添加（顺带下载）、看现状、请自动更新建知识库、移除（下载的插件一起删）。
"""

from __future__ import annotations

import contextlib
import hashlib
import shutil
import zipfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from aiohttp.test_utils import TestClient, TestServer

from deployment import FULL_CONFIG_DIR
from jianshanagent import _bootstrap
from jianshanagent.core import manifest as manifest_module
from jianshanagent.core.catalog import (
    CatalogError,
    CatalogSettings,
    CatalogSite,
    SiteCatalog,
    install_site_wheel,
    parse_catalog,
    requirement_problem,
    uninstall,
)
from jianshanagent.core.config import (
    WIKI_FILE,
    WIKIS_ADDED,
    added_wikis,
    load_config,
    write_added_wikis,
    write_override,
)
from jianshanagent.core.manifest import SITES_DIR_ENV, installed_sites, read_sites_state
from jianshanagent.plugins.knowledge.settings import KB_DIR_ENV
from jianshanagent.plugins.knowledge.sync import BUILD_FILE, read_build
from jianshanagent.runtime import Runtime
from jianshanagent.testing.sites import register_plugins

OWNER = "JianShanAgent"
TOKEN = "test-token-0123456789"
WRITE = {"X-Requested-With": "jianshanagent-admin"}
DEMO = {
    "id": "huiji_demo",
    "platform": "huiji",
    "name": "示例 wiki",
    "description": "灰机上的\n  示例站",
    "repo": f"{OWNER}/site-huiji-demo",
    "wiki": {
        "id": "demo",
        "label": "示例站",
        "api_url": "https://demo.huiji.example/api.php",
        "auth_header": "X-authkey",
    },
}
CATALOG = {"format": 1, "sites": [DEMO]}


def site_of(raw: dict[str, Any] = DEMO) -> CatalogSite:
    sites, problems = parse_catalog({"format": 1, "sites": [raw]}, owner=OWNER)
    assert not problems
    return sites[0]


def make_site_wheel(
    folder: Path,
    version: str = "0.2.0",
    *,
    site_id: str = "huiji_demo",
    platform: str = "huiji",
    requires: tuple[str, ...] = ("jianshanagent>=0.3.0",),
    tag: str = "py3-none-any",
    files: dict[str, str] | None = None,
) -> Path:
    """一个最小的站点插件安装包：清单、对通用配置的一点补充、一个模块。"""
    folder.mkdir(parents=True, exist_ok=True)
    package = f"jianshanagent_site_{site_id}"
    info = f"{package}-{version}.dist-info"
    manifest = {"id": site_id, "kind": "site", "name": "示例 wiki", "capabilities": ["extend"]}
    if platform:
        manifest["platform"] = platform
    config = {"extend": {"plugin.knowledge": {"clean": {"remove_classes": ["demo-nav"]}}}}
    metadata = ["Metadata-Version: 2.4", f"Name: jianshanagent-site-{site_id.replace('_', '-')}"]
    metadata += [f"Version: {version}", "Requires-Python: >=3.12"]
    metadata += [f"Requires-Dist: {item}" for item in requires]
    content = {
        f"{package}/__init__.py": f'VERSION = "{version}"\n',
        f"{package}/plugin.yaml": yaml.safe_dump(manifest, allow_unicode=True),
        f"{package}/config.yaml": yaml.safe_dump(config, allow_unicode=True),
        f"{info}/METADATA": "\n".join(metadata) + "\n",
        f"{info}/WHEEL": f"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: {tag}\n",
        **(files or {}),
    }
    path = folder / f"{package}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as archive:
        for name, text in content.items():
            archive.writestr(name, text)
    return path


@pytest.fixture(autouse=True)
def running_core(monkeypatch: pytest.MonkeyPatch) -> None:
    """核心算是 0.3.0；环境里真装着的站点插件当作没装。"""
    monkeypatch.setattr(_bootstrap, "running_version", lambda: "0.3.0")
    register_plugins(monkeypatch)


@pytest.fixture
def sites_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "sites"
    monkeypatch.setenv(SITES_DIR_ENV, str(directory))
    yield directory
    # 测试里下载的包不留在导入路径和已导入的模块里
    for site_id in list(read_sites_state(directory)):
        uninstall(directory, site_id)
    manifest_module.clear_caches()


class GitHub:
    """假的 GitHub：一份目录、各个站点插件仓库最新的 Release 和安装包。"""

    def __init__(self, folder: Path, catalog: dict[str, Any] | None = None):
        self.folder = folder
        self.catalog: Any = catalog or CATALOG
        self.wheels: dict[str, Path] = {}
        self.asked: list[str] = []
        self.down = False
        #: 给出去的校验和（None = 照实给）
        self.digest: str | None = None

    def release(self, repo: str, wheel: Path) -> None:
        self.wheels[repo] = wheel

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.asked.append(path)
        if self.down:
            return httpx.Response(503)
        if path == f"/repos/{OWNER}/catalog/contents/sites.yaml":
            assert request.url.params["ref"] == "main"
            return httpx.Response(200, text=yaml.safe_dump(self.catalog, allow_unicode=True))
        for repo, wheel in self.wheels.items():
            version = wheel.name.split("-")[1]
            if path == f"/repos/{repo}/releases/latest":
                digest = self.digest or hashlib.sha256(wheel.read_bytes()).hexdigest()
                asset = {
                    "name": wheel.name,
                    "size": wheel.stat().st_size,
                    "digest": f"sha256:{digest}",
                    "browser_download_url": f"https://github.test/dl/{wheel.name}",
                }
                return httpx.Response(200, json={"tag_name": f"v{version}", "assets": [asset]})
            if path.endswith(f"/dl/{wheel.name}"):  # 走镜像时前面多一截
                return httpx.Response(200, content=wheel.read_bytes())
        return httpx.Response(404)


def catalog_with(github: GitHub, **settings: Any) -> SiteCatalog:
    chosen = CatalogSettings(api_url="https://api.github.test", **settings)
    return SiteCatalog(lambda: chosen, transport=httpx.MockTransport(github))


# ---------- 目录 ----------


def test_catalog_keeps_good_entries_and_says_why_it_skipped_the_others() -> None:
    elsewhere = {**DEMO, "id": "huiji_other", "repo": "someone/site-huiji-other"}
    misplaced = {**DEMO, "id": "demo2"}  # 灰机上的站，id 却没带平台
    no_address = {**DEMO, "id": "huiji_blank", "wiki": {"id": "blank"}}
    future = {**DEMO, "id": "fandom_demo", "platform": "fandom"}
    own = {
        "id": "thwiki",
        "name": "THBWiki",
        "repo": f"{OWNER}/site-thwiki",
        "wiki": {"api_url": "https://thwiki.example/api.php", "article_path": "/$1"},
    }
    sites, problems = parse_catalog(
        {"format": 1, "sites": [DEMO, DEMO, elsewhere, misplaced, no_address, future, own, "x"]},
        owner=OWNER,
    )
    assert [site.id for site in sites] == ["huiji_demo", "fandom_demo", "thwiki"]
    demo, fandom, thwiki = sites
    assert demo.description == "灰机上的 示例站" and demo.package == "jianshanagent_site_huiji_demo"
    # 挂上时写进 core.wiki.yaml 的那一项：地址照目录里的，名字可以另起
    assert demo.wiki_entry("mine", "") == {
        "id": "mine",
        "api_url": "https://demo.huiji.example/api.php",
        "auth_header": "X-authkey",
        "site": "huiji_demo",
    }
    # 核心还不认识的平台：列出来，但是用不了
    assert "先更新核心" in fandom.unavailable and not demo.unavailable
    # 自己架的 MediaWiki 站：不写平台，wiki 的名字默认就是它的 id
    assert (thwiki.platform, thwiki.wiki_id) == ("mediawiki", "thwiki")
    assert thwiki.wiki_entry("thwiki", "THB")["article_path"] == "/$1"
    assert len(problems) == 5
    assert any("写了两次" in item for item in problems)
    assert any("不在 JianShanAgent 这个组织下" in item for item in problems)
    assert any("id 却不是以 huiji_ 开头" in item for item in problems)
    assert any("wiki.api_url" in item for item in problems)

    with pytest.raises(CatalogError, match="只认第 1 版"):
        parse_catalog({"format": 2, "sites": []}, owner=OWNER)
    with pytest.raises(CatalogError, match="格式不对"):
        parse_catalog(["huiji_demo"], owner=OWNER)


async def test_catalog_is_read_once_in_a_while_and_survives_an_outage(tmp_path: Path) -> None:
    github = GitHub(tmp_path)
    catalog = catalog_with(github)
    assert [site.id for site in await catalog.load()] == ["huiji_demo"]
    await catalog.load()
    assert len(github.asked) == 1 and catalog.fetched_at  # 几分钟内不重读
    await catalog.load(refresh=True)
    assert len(github.asked) == 2

    # 读不成：之前读到的还在，原因记着
    github.down = True
    assert [site.id for site in await catalog.load(refresh=True)] == ["huiji_demo"]
    assert "503" in catalog.error
    with pytest.raises(CatalogError, match="站点目录里没有「nope」"):
        await catalog.site("nope")

    # 从没读到过：报错
    with pytest.raises(CatalogError, match="站点目录读不了"):
        await catalog_with(github).load()
    # 关掉了：不读
    assert await catalog_with(github, enabled=False).load() == []


# ---------- 下载 ----------


def test_requirements_are_checked_against_this_environment() -> None:
    assert requirement_problem("jianshanagent>=0.3.0") == ""
    assert requirement_problem("jianshanagent (>=0.2,<1)") == ""
    assert "先更新核心" in requirement_problem("jianshanagent>=0.4")
    assert requirement_problem("httpx>=0.20") == ""
    assert "环境里没有装" in requirement_problem("not-a-real-package>=1")
    assert requirement_problem("pytest>=8; extra == 'dev'") == ""  # 可选依赖不算


async def test_site_plugin_is_downloaded_verified_and_found_without_a_restart(
    tmp_path: Path, sites_dir: Path
) -> None:
    github = GitHub(tmp_path)
    github.release(DEMO["repo"], make_site_wheel(tmp_path / "dist"))
    catalog = catalog_with(github, download_prefix="https://mirror.test/")
    site = await catalog.site("huiji_demo")
    assert "huiji_demo" not in installed_sites(tmp_path)

    assert await catalog.install(site, sites_dir) == "0.2.0"
    assert read_sites_state(sites_dir)["huiji_demo"]["repo"] == DEMO["repo"]
    found = installed_sites(tmp_path)["huiji_demo"]
    assert found.platform == "huiji" and found.package == "jianshanagent_site_huiji_demo"
    assert found.path == sites_dir / "huiji_demo" / "0.2.0" / "jianshanagent_site_huiji_demo"
    assert manifest_module.site_source(found, tmp_path) == "downloaded"
    assert not (sites_dir / ".tmp").exists()

    # 已经是这个版本：不再下载
    asked = len(github.asked)
    assert await catalog.install(site, sites_dir) == "0.2.0"
    assert not any("/dl/" in path for path in github.asked[asked:])
    assert any(path.startswith("/https://github.test/dl/") for path in github.asked)  # 走了镜像

    # 新版本：换过去，上一个版本留着（正在跑的进程可能还在用），更早的清掉
    for version in ("0.2.1", "0.3.0"):
        github.release(DEMO["repo"], make_site_wheel(tmp_path / "dist", version))
        assert await catalog.install(site, sites_dir) == version
    assert sorted(p.name for p in (sites_dir / "huiji_demo").iterdir()) == ["0.2.1", "0.3.0"]

    assert uninstall(sites_dir, "huiji_demo") and not uninstall(sites_dir, "huiji_demo")
    assert "huiji_demo" not in installed_sites(tmp_path) and not (sites_dir / "huiji_demo").exists()


@pytest.mark.parametrize(
    ("wheel", "problem"),
    [
        ({"requires": ("jianshanagent>=0.9",)}, "先更新核心"),
        ({"tag": "cp312-cp312-linux_x86_64"}, "不是纯 Python"),
        ({"files": {"jianshanagent/core/evil.py": "x = 1\n"}}, "不该有的文件"),
        ({"files": {"jianshanagent_site_huiji_demo/bad.py": "def (:\n"}}, "编译不过"),
        ({"platform": ""}, "清单不合"),  # id 带着平台，清单里却没写
        ({"site_id": "huiji_other"}, "不该有的文件"),  # 是别的插件的包
    ],
)
def test_unsuitable_packages_are_not_installed(
    tmp_path: Path, sites_dir: Path, wheel: dict[str, Any], problem: str
) -> None:
    path = make_site_wheel(tmp_path / "dist", **wheel)
    with pytest.raises(CatalogError, match=problem):
        install_site_wheel(path, site_of(), "0.2.0", sites_dir)
    assert not (sites_dir / "huiji_demo").exists() or not list((sites_dir / "huiji_demo").iterdir())


async def test_a_package_that_fails_its_checksum_or_has_none_is_not_installed(
    tmp_path: Path, sites_dir: Path
) -> None:
    github = GitHub(tmp_path)
    github.release(DEMO["repo"], make_site_wheel(tmp_path / "dist"))
    github.digest = "0" * 64
    catalog = catalog_with(github)
    site = await catalog.site("huiji_demo")
    with pytest.raises(CatalogError, match="校验和对不上"):
        await catalog.install(site, sites_dir)
    assert read_sites_state(sites_dir) == {} and "huiji_demo" not in installed_sites(tmp_path)

    github.wheels.clear()
    with pytest.raises(CatalogError, match="还没有发过版本"):
        await catalog.install(site, sites_dir)


# ---------- 后台加的 wiki ----------


def deployment(tmp_path: Path) -> tuple[Path, Path]:
    """（配置目录，覆写目录）：部署里写着一个纯 MediaWiki 的 wiki「main」。"""
    root = tmp_path / "instance" / "config"
    shutil.copytree(FULL_CONFIG_DIR, root, ignore=shutil.ignore_patterns("local", "secrets.yaml"))
    return root, tmp_path / "overrides"


async def test_added_wikis_follow_the_deployed_ones_and_a_broken_one_is_skipped(
    tmp_path: Path, sites_dir: Path
) -> None:
    root, overrides = deployment(tmp_path)
    overrides.mkdir()
    github = GitHub(tmp_path)
    github.release(DEMO["repo"], make_site_wheel(tmp_path / "dist"))
    catalog = catalog_with(github)
    site = await catalog.site("huiji_demo")
    await catalog.install(site, sites_dir)

    config = load_config(root, overrides)
    write_added_wikis(config, [site.wiki_entry("demo", "示例站")])
    config = load_config(root, overrides)
    assert [(entry.id, entry.added) for entry in config.wikis] == [("main", False), ("demo", True)]
    assert (config.platforms, config.sites) == (["mediawiki", "huiji"], ["huiji_demo"])
    # 它不算对配置的改动：覆写里看不到，页面上的名单是只读的一整张
    assert WIKI_FILE not in config.overrides and not config.override_errors
    assert [item["id"] for item in config.get(WIKI_FILE)["wikis"]] == ["main", "demo"]
    assert WIKIS_ADDED not in config.get(WIKI_FILE)
    # 它的站点插件的补充只对它那一份生效
    assert "demo-nav" in config.for_wiki("demo").plugin("knowledge")["clean"]["remove_classes"]
    assert "demo-nav" not in config.plugin("knowledge")["clean"]["remove_classes"]

    # 在「配置」页里改这份配置的别的项：加的 wiki 还在
    write_override(config, WIKI_FILE, {**config.get(WIKI_FILE), "about": "一个示例"})
    config = load_config(root, overrides)
    assert config.overrides[WIKI_FILE] == {"about": "一个示例"}
    assert [entry.id for entry in config.wikis] == ["main", "demo"]
    write_override(config, WIKI_FILE, config.base_files[WIKI_FILE])  # 改回默认
    assert [item["id"] for item in added_wikis(load_config(root, overrides))] == ["demo"]

    # 名字和部署写的重了：以部署写的为准
    write_added_wikis(config, [site.wiki_entry("main", ""), site.wiki_entry("demo", "")])
    config = load_config(root, overrides)
    assert [entry.id for entry in config.wikis] == ["main", "demo"] and not config.wikis[0].added
    assert "「main」没有挂上" in config.override_errors[WIKI_FILE]

    # 站点插件没了：跳过它，不拦启动；不看覆写时它也不在
    uninstall(sites_dir, "huiji_demo")
    config = load_config(root, overrides)
    assert [entry.id for entry in config.wikis] == ["main"]
    assert "没有叫「huiji_demo」的站点插件" in config.override_errors[WIKI_FILE]
    assert len(added_wikis(config)) == 2
    assert [e.id for e in load_config(root, overrides, with_overrides=False).wikis] == ["main"]

    write_added_wikis(config, [])
    assert not (overrides / "core.wiki.yaml").exists()


# ---------- 管理后台的接口 ----------


class Env:
    def __init__(self, runtime: Runtime, client: TestClient, github: GitHub, tmp_path: Path):
        self.runtime = runtime
        self.client = client
        self.github = github
        self.tmp_path = tmp_path

    async def get(self, path: str, status: int = 200) -> Any:
        response = await self.client.get(path)
        assert response.status == status, await response.text()
        return await response.json()

    async def send(self, method: str, path: str, body: Any = None, status: int = 200) -> Any:
        response = await self.client.request(method, path, json=body, headers=WRITE)
        assert response.status == status, await response.text()
        return await response.json()


@contextlib.asynccontextmanager
async def running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, wikis: bool = True
) -> AsyncIterator[Env]:
    """起一个带管理后台的机器人。``wikis=False``：部署的配置里一个 wiki 都没写（新的实例）。"""
    root, overrides = deployment(tmp_path)
    if not wikis:
        (root / "core.wiki.yaml").write_text('name: "示例"\nwikis: []\n', encoding="utf-8")
    admin = root / "core.admin.yaml"
    admin.write_text(
        admin.read_text("utf-8").replace(
            "token_file: data/admin/token", f"token_file: {tmp_path / 'admin' / 'token'}"
        ),
        encoding="utf-8",
    )
    (root / "secrets.yaml").write_text(
        f"llm:\n  api_key: tp-test\nembedding:\n  api_key: tp-test\nadmin:\n  token: {TOKEN}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("JIANSHANAGENT_CONFIG_DIR", str(root))
    monkeypatch.setenv("JIANSHANAGENT_CONFIG_OVERRIDES_DIR", str(overrides))
    monkeypatch.setenv(KB_DIR_ENV, str(tmp_path / "kb"))
    monkeypatch.delenv("JIANSHANAGENT_POSTGRES_DSN", raising=False)
    monkeypatch.delenv("JIANSHANAGENT_LLM_API_KEY", raising=False)
    monkeypatch.setattr("jianshanagent.runtime.setup_logging", lambda **_kwargs: None)
    github = GitHub(tmp_path)
    github.release(DEMO["repo"], make_site_wheel(tmp_path / "dist"))
    runtime = Runtime(dry_run=True)
    runtime.catalog = catalog_with(github)
    await runtime.prepare()
    client = TestClient(TestServer(runtime.build_app()))
    await client.start_server()
    response = await client.post("/api/login", json={"token": TOKEN})
    assert response.status == 200
    try:
        yield Env(runtime, client, github, tmp_path)
    finally:
        await client.close()
        await runtime.stop()


@pytest.fixture
async def env(
    tmp_path: Path, sites_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Env]:
    async with running(tmp_path, monkeypatch) as started:
        yield started


async def test_adding_a_wiki_from_the_catalog_downloads_its_plugin_and_takes_effect(
    env: Env, sites_dir: Path
) -> None:
    before = await env.get("/api/wikis")
    assert [wiki["id"] for wiki in before["wikis"]] == ["main"] and before["writable"]
    assert {item["id"] for item in before["platforms"]} == {"huiji", "bwiki", "mediawiki"}
    assert before["wikis"][0] | {"kb": None} == {
        "id": "main",
        "label": "main",
        "primary": True,
        "added": False,
        "platform": "mediawiki",
        "site": None,
        "api_url": "https://wiki.example/api.php",
        "auth_header": "",
        "auth_key_set": False,
        "running": True,
        "kb": None,
    }
    listed = await env.get("/api/wikis/catalog")
    assert listed["repo"] == "JianShanAgent/catalog" and not listed["error"]
    (site,) = listed["sites"]
    assert site["id"] == "huiji_demo" and site["installed"] is None and site["wiki"] is None
    assert site["auth_header"] == "X-authkey"

    # 网页上只能起名字：地址照目录里的
    body = {"site": "huiji_demo", "label": "示例", "api_url": "https://evil.example/api.php"}
    added = await env.send("POST", "/api/wikis", body)
    assert added["downloaded"] == "0.2.0"
    assert added["added"] | {"kb": None} == {
        "id": "demo",
        "label": "示例",
        "primary": False,
        "added": True,
        "platform": "huiji",
        "site": {
            "id": "huiji_demo",
            "name": "示例 wiki",
            "source": "downloaded",
            "version": "0.2.0",
        },
        "api_url": "https://demo.huiji.example/api.php",
        "auth_header": "X-authkey",
        "auth_key_set": False,
        "running": True,
        "kb": None,
    }
    # 不用重启：正在运行的那一套里已经有它了
    assert [entry.id for entry in env.runtime.config.wikis] == ["main", "demo"]
    assert env.runtime.config.wiki("demo").site.id == "huiji_demo"
    (site,) = (await env.get("/api/wikis/catalog"))["sites"]
    assert site["installed"]["source"] == "downloaded" and site["wiki"] == "demo"

    # 同一个站不能挂两次；名字不能和已有的重
    again = await env.send("POST", "/api/wikis", {"site": "huiji_demo", "id": "demo2"}, 409)
    assert "已经挂着了" in again["error"]
    await env.send("POST", "/api/wikis", {"site": "nope"}, 404)

    # 「配置」页里那张名单是只读的，照样能存别的项
    wiki_config = await env.get("/api/config/core.wiki")
    assert [item["id"] for item in wiki_config["effective"]["wikis"]] == ["main", "demo"]
    values = {**wiki_config["effective"], "about": "示例"}
    await env.send("PUT", "/api/config/core.wiki", {"values": values})
    assert [entry.id for entry in env.runtime.config.wikis] == ["main", "demo"]
    values["wikis"] = values["wikis"][:1]
    await env.send("PUT", "/api/config/core.wiki", {"values": values}, 400)

    # 部署写的不能在这里移除；加的可以，下载的站点插件跟着删掉
    refused = await env.send("DELETE", "/api/wikis/main", status=409)
    assert "部署的配置文件" in refused["error"]
    removed = await env.send("DELETE", "/api/wikis/demo")
    assert removed == {"removed": "demo", "site_removed": True, "status": removed["status"]}
    assert [entry.id for entry in env.runtime.config.wikis] == ["main"]
    assert read_sites_state(sites_dir) == {} and not (sites_dir / "huiji_demo").exists()
    await env.send("DELETE", "/api/wikis/demo", status=404)


async def test_a_new_instance_starts_without_a_wiki_and_gets_its_first_one_here(
    tmp_path: Path, sites_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with running(tmp_path, monkeypatch, wikis=False) as env:
        assert (await env.get("/api/wikis"))["wikis"] == []
        hub = env.runtime.host.services["wiki_query"]
        assert hub.clients == () and await hub.search("示例") == []

        added = await env.send("POST", "/api/wikis", {"site": "huiji_demo"})
        assert added["added"]["id"] == "demo" and added["added"]["primary"]
        hub = env.runtime.host.services["wiki_query"]
        assert [client.wiki.id for client in hub.clients] == ["demo"]

        # 移除以后又是一个都没有，照样运行
        await env.send("DELETE", "/api/wikis/demo")
        assert env.runtime.config.wikis == ()
        assert env.runtime.host.services["wiki_query"].clients == ()


async def test_a_wiki_that_cannot_be_added_leaves_nothing_behind(
    env: Env, sites_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 站点插件装不上
    env.github.digest = "0" * 64
    failed = await env.send("POST", "/api/wikis", {"site": "huiji_demo"}, 409)
    assert "校验和对不上" in failed["error"]
    env.github.digest = None

    # 名字不合规矩：还没下载就拦下
    await env.send("POST", "/api/wikis", {"site": "huiji_demo", "id": "Bad Name"}, 400)
    taken = await env.send("POST", "/api/wikis", {"site": "huiji_demo", "id": "main"}, 409)
    assert "换一个名字" in taken["error"]
    assert read_sites_state(sites_dir) == {}

    # 加上以后装不起来：撤回，下载的也删掉
    real = env.runtime.reload
    calls = 0

    async def reload() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("装不起来")
        await real()

    monkeypatch.setattr(env.runtime, "reload", reload)
    rejected = await env.send("POST", "/api/wikis", {"site": "huiji_demo"}, 409)
    assert "已经撤回" in rejected["error"]
    assert read_sites_state(sites_dir) == {} and added_wikis(env.runtime.config) == []
    assert [wiki["id"] for wiki in (await env.get("/api/wikis"))["wikis"]] == ["main"]


async def test_the_knowledge_base_is_built_only_when_asked(env: Env, tmp_path: Path) -> None:
    await env.send("POST", "/api/wikis", {"site": "huiji_demo"})
    wiki = (await env.get("/api/wikis"))["wikis"][1]
    assert wiki["kb"] == {"state": "none", "requested_at": None, "started_at": None, "problem": ""}
    assert not (tmp_path / "kb" / "demo" / BUILD_FILE).exists()  # 加上不等于建库

    # 没有嵌入接口建不了
    embedding = (await env.get("/api/config/core.embedding"))["effective"]
    off = {"values": {**embedding, "enabled": False}}
    await env.send("PUT", "/api/config/core.embedding", off)
    refused = await env.send("POST", "/api/wikis/demo/kb", status=409)
    assert "嵌入接口" in refused["error"]
    await env.send("PUT", "/api/config/core.embedding", {"values": embedding})

    asked = await env.send("POST", "/api/wikis/demo/kb")
    assert asked["kb"]["state"] == "requested" and asked["sync_alive"] is False
    request = read_build(tmp_path / "kb" / "demo")
    assert request is not None and request["requested_at"] and not request["started_at"]
    await env.send("POST", "/api/wikis/demo/kb", status=409)  # 已经请过了
    await env.send("POST", "/api/wikis/nope/kb", status=404)

    # 还没开始建的可以撤回；移除 wiki 时也一并撤回
    assert (await env.send("DELETE", "/api/wikis/demo/kb"))["kb"]["state"] == "none"
    await env.send("POST", "/api/wikis/demo/kb")
    await env.send("DELETE", "/api/wikis/demo")
    assert read_build(tmp_path / "kb" / "demo") is None


async def test_a_downloaded_plugin_is_updated_on_request_and_the_bot_restarts(
    env: Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await env.send("POST", "/api/wikis", {"site": "huiji_demo"})
    stops: list[bool] = []
    monkeypatch.setattr(env.runtime, "request_stop", lambda *, restart=False: stops.append(restart))

    same = await env.send("POST", "/api/wikis/sites/huiji_demo/update")
    assert same == {"version": "0.2.0", "updated": False, "restarting": False}

    env.github.release(DEMO["repo"], make_site_wheel(tmp_path / "dist", "0.2.1"))
    newer = await env.send("POST", "/api/wikis/sites/huiji_demo/update")
    assert newer == {"version": "0.2.1", "updated": True, "restarting": True}
    wiki = (await env.get("/api/wikis"))["wikis"][1]
    assert wiki["site"]["version"] == "0.2.1"

    # 随部署走的站点插件不在这里更新
    refused = await env.send("POST", "/api/wikis/sites/huiji_yys/update", status=409)
    assert "不是在这里下载的" in refused["error"]
