"""核心的更新（``_bootstrap.py``、``core/update.py``）。

- 引导：有确认过的更新包、而且比环境里装的新，才用它；刚装上的只有机器人进程去试，
  试不成退回原来的版本；从源码运行、按发布流程部署的不用更新包。
- 查：认得出主仓库最新的版本，查不成只记原因。
- 装：安装包要对得上版本和校验和、是纯 Python 的、依赖和环境里装的一样，才解到更新目录、记成待确认。
- 管理后台的接口：看现状、点更新、还原。

这里不真的换掉正在跑测试的这个包（``_switch`` 换成只记一笔）；真换、真重启的那一遍在发版前手动做。
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path
from typing import Any

import httpx
import pytest
from aiohttp.test_utils import TestClient, TestServer

from jianshanagent import _bootstrap
from jianshanagent.core.update import (
    Release,
    UpdateError,
    Updater,
    UpdateSettings,
    install_wheel,
    parse_release,
    restart_needed,
    wheel_name,
)
from jianshanagent.runtime import Runtime

BASE = "0.2.0"
REQUIRES = ["httpx>=0.27", "pyyaml>=6.0", "pytest>=8; extra == 'dev'"]
TOKEN = "test-token-0123456789"
WRITE = {"X-Requested-With": "jianshanagent-admin"}


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """一个能用更新包的环境：核心是装成包的 0.2.0，更新目录在临时目录里。"""
    directory = tmp_path / "core"
    monkeypatch.setenv(_bootstrap.UPDATE_DIR_ENV, str(directory))
    monkeypatch.delenv(_bootstrap.RELEASE_ENV, raising=False)
    monkeypatch.setattr(_bootstrap, "_editable", lambda: False)
    monkeypatch.setattr(_bootstrap, "base_version", lambda: BASE)
    monkeypatch.setattr(_bootstrap, "_active", "")
    monkeypatch.setattr(_bootstrap, "_trial", False)
    # 不真的把正在跑测试的包换掉：只记下「现在用的是哪个版本」
    monkeypatch.setattr(
        _bootstrap, "_switch", lambda _dir, version: setattr(_bootstrap, "_active", version)
    )
    monkeypatch.setattr("jianshanagent.core.update.metadata.requires", lambda _name: REQUIRES)
    return directory


def make_wheel(
    folder: Path,
    version: str,
    *,
    name: str = "jianshanagent",
    requires: list[str] | None = None,
    python: str = ">=3.12",
    tag: str = "py3-none-any",
    files: dict[str, str] | None = None,
) -> Path:
    """一个最小的安装包：包里只有 ``__init__.py`` 和一个模块。"""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / wheel_name(version)
    info = f"jianshanagent-{version}.dist-info"
    lines = ["Metadata-Version: 2.4", f"Name: {name}", f"Version: {version}"]
    lines += [f"Requires-Python: {python}"]
    lines += [f"Requires-Dist: {item}" for item in (REQUIRES if requires is None else requires)]
    content = {
        "jianshanagent/__init__.py": f'VERSION = "{version}"\n',
        "jianshanagent/main.py": "def main():\n    return 0\n",
        f"{info}/METADATA": "\n".join(lines) + "\n",
        f"{info}/WHEEL": f"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: {tag}\n",
        **(files or {}),
    }
    with zipfile.ZipFile(path, "w") as archive:
        for member, text in content.items():
            archive.writestr(member, text)
    return path


def put(directory: Path, version: str) -> None:
    """更新目录里放一个已经解好的版本。"""
    package = _bootstrap.package_dir(directory, version)
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", "utf-8")


# ---------- 引导 ----------


def test_versions_are_compared_as_numbers_and_only_plain_releases_count() -> None:
    assert _bootstrap.parse_version("v0.3.1") == (0, 3, 1)
    assert _bootstrap.newer("0.10.0", "0.9.9") and _bootstrap.newer("1.0", "0.99.99")
    assert not _bootstrap.newer("0.2.0", "0.2") and not _bootstrap.newer("0.2.0", "0.2.0")
    # 认不出来的（预发布、开发版）不算新：宁可不更新
    for odd in ("0.3.0rc1", "0.3.0.dev", "main", ""):
        assert _bootstrap.parse_version(odd) is None
        assert not _bootstrap.newer(odd, "0.1.0") and not _bootstrap.newer("9.9.9", odd)


def test_only_a_confirmed_newer_package_is_used_at_import(home: Path) -> None:
    assert _bootstrap.mode() == (_bootstrap.MODE_OVERLAY, "")
    _bootstrap.activate()  # 没有更新目录：照环境里装的
    assert _bootstrap.running_version() == BASE

    put(home, "0.3.0")
    _bootstrap.write_state(home, {"current": "0.3.0", "status": "pending", "attempts": 0})
    _bootstrap.activate()  # 刚装上、还没确认的：别的进程不碰
    assert _bootstrap.running_version() == BASE and _bootstrap.confirmed_version(home) == ""

    _bootstrap.write_state(home, {"current": "0.3.0", "status": "ok"})
    _bootstrap.activate()
    assert _bootstrap.running_version() == "0.3.0"

    # 环境里装的版本追上来了（重建了镜像）：更新包不再用
    _bootstrap._active = ""
    _bootstrap.base_version = lambda: "0.3.0"  # type: ignore[assignment]
    _bootstrap.activate()
    assert _bootstrap.running_version() == "0.3.0" and _bootstrap._active == ""


def test_broken_state_or_missing_files_fall_back_to_the_installed_version(home: Path) -> None:
    home.mkdir(parents=True)
    (home / _bootstrap.STATE_FILE).write_text("{not json", "utf-8")
    _bootstrap.activate()
    assert _bootstrap.running_version() == BASE
    # 格式号不认识（以后的版本写的）：当作没有
    (home / _bootstrap.STATE_FILE).write_text(
        json.dumps({"format": 99, "current": "0.9.0", "status": "ok"}), "utf-8"
    )
    assert _bootstrap.read_state(home) == {}
    # 记着要用，包却不在
    _bootstrap.write_state(home, {"current": "0.3.0", "status": "ok"})
    _bootstrap.activate()
    assert _bootstrap.running_version() == BASE


def test_source_checkouts_and_managed_deployments_never_use_a_package(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    put(home, "0.3.0")
    _bootstrap.write_state(home, {"current": "0.3.0", "status": "ok"})
    monkeypatch.setattr(_bootstrap, "_editable", lambda: True)
    assert _bootstrap.mode()[0] == _bootstrap.MODE_SOURCE
    _bootstrap.activate()
    assert _bootstrap.running_version() == BASE  # 不然更新包会盖住正在改的源码

    monkeypatch.setattr(_bootstrap, "_editable", lambda: False)
    monkeypatch.setenv(_bootstrap.RELEASE_ENV, "7d4941f")
    assert _bootstrap.mode()[0] == _bootstrap.MODE_MANAGED
    _bootstrap.activate()
    assert _bootstrap.running_version() == BASE and not restart_needed()

    # 没有实例目录（不在实例里运行，也没设环境变量）：没有地方放
    monkeypatch.delenv(_bootstrap.RELEASE_ENV)
    monkeypatch.delenv(_bootstrap.UPDATE_DIR_ENV)
    monkeypatch.delenv(_bootstrap.CONFIG_DIR_ENV, raising=False)
    monkeypatch.chdir(home.parent)
    assert _bootstrap.update_dir() is None and _bootstrap.mode()[0] == _bootstrap.MODE_NONE
    (home.parent / "config").mkdir()
    assert _bootstrap.update_dir() == home.parent / "data" / "core"


def test_the_bot_tries_a_new_package_and_confirms_it_once_started(home: Path) -> None:
    put(home, "0.3.0")
    put(home, "0.4.0")
    _bootstrap.write_state(
        home, {"current": "0.4.0", "status": "pending", "attempts": 0, "previous": "0.3.0"}
    )
    # 别的进程这时用的是上一个确认过的
    assert _bootstrap.confirmed_version(home) == "0.3.0"

    _bootstrap._begin_trial()
    assert _bootstrap.trial() and _bootstrap.running_version() == "0.4.0"
    assert _bootstrap.read_state(home)["attempts"] == 1

    assert _bootstrap.confirm() is True  # 启动完成
    state = _bootstrap.read_state(home)
    assert (state["status"], state["attempts"]) == ("ok", 0) and not _bootstrap.trial()
    assert _bootstrap.confirmed_version(home) == "0.4.0"
    assert _bootstrap.confirm() is False  # 没有在试的：什么都不做


def test_a_package_that_never_gets_ready_is_given_up(home: Path) -> None:
    put(home, "0.3.0")
    put(home, "0.4.0")
    _bootstrap.write_state(
        home, {"current": "0.4.0", "status": "pending", "attempts": 0, "previous": "0.3.0"}
    )
    for attempt in range(1, _bootstrap.MAX_ATTEMPTS + 1):  # 每次都没走到启动完成就没了
        _bootstrap._active, _bootstrap._trial = "", False
        _bootstrap._begin_trial()
        assert _bootstrap.read_state(home)["attempts"] == attempt
    _bootstrap._active, _bootstrap._trial = "", False
    _bootstrap._begin_trial()  # 再来一次：不试了，退回上一个确认过的
    state = _bootstrap.read_state(home)
    assert (state["current"], state["status"]) == ("0.3.0", "ok") and not _bootstrap.trial()
    assert state["last_failure"]["version"] == "0.4.0"
    assert "没有走到启动完成" in state["last_failure"]["error"]

    # 没有上一个可退：退回环境里装的
    _bootstrap.write_state(home, {"current": "0.4.0", "status": "pending", "attempts": 99})
    _bootstrap._begin_trial()
    assert _bootstrap.read_state(home)["current"] == "" and _bootstrap.confirmed_version(home) == ""


def test_a_package_that_fails_on_start_is_given_up_and_the_bot_starts_again(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    put(home, "0.3.0")
    _bootstrap.write_state(home, {"current": "0.3.0", "status": "pending", "attempts": 0})

    def broken_main() -> int:
        raise RuntimeError("新版本一启动就出错")

    monkeypatch.setattr("jianshanagent.main.main", broken_main)
    restarted: list[list[str]] = []

    def fake_execv(_path: str, argv: list[str]) -> None:
        restarted.append(argv)
        raise SystemExit(0)

    monkeypatch.setattr(_bootstrap.os, "execv", fake_execv)
    with pytest.raises(SystemExit):
        _bootstrap.launch()
    assert restarted and restarted[0][1:3] == ["-m", "jianshanagent"]
    state = _bootstrap.read_state(home)
    assert state["current"] == "" and "新版本一启动就出错" in state["last_failure"]["error"]

    # 没有在试更新包时，启动出错照常抛出去，不重启
    _bootstrap._trial = False
    restarted.clear()
    with pytest.raises(RuntimeError):
        _bootstrap.launch()
    assert restarted == []


def test_other_long_running_processes_notice_a_confirmed_change(home: Path) -> None:
    assert not restart_needed()
    put(home, "0.3.0")
    _bootstrap.write_state(home, {"current": "0.3.0", "status": "pending", "attempts": 1})
    assert not restart_needed()  # 还在试：不跟
    _bootstrap.write_state(home, {"current": "0.3.0", "status": "ok"})
    assert restart_needed()  # 机器人确认了：知识库自动更新也该换过去
    _bootstrap._active = "0.3.0"
    assert not restart_needed()
    _bootstrap.write_state(home, {"current": "", "status": ""})  # 后台里点了还原
    assert restart_needed()


# ---------- 查 ----------


def release_json(version: str, wheel: Path | None = None, **extra: Any) -> dict[str, Any]:
    assets: list[dict[str, Any]] = []
    if wheel is not None:
        assets.append(
            {
                "name": wheel.name,
                "size": wheel.stat().st_size,
                "browser_download_url": f"https://github.test/dl/{wheel.name}",
                "digest": "sha256:" + hashlib.sha256(wheel.read_bytes()).hexdigest(),
            }
        )
    return {
        "tag_name": f"v{version}",
        "name": f"v{version}",
        "body": "改了这些：\n- 一\n- 二",
        "published_at": "2026-11-12T08:00:00Z",
        "html_url": f"https://github.test/releases/v{version}",
        "assets": assets,
        **extra,
    }


def updater_with(handler: Any, **settings: Any) -> Updater:
    chosen = UpdateSettings(api_url="https://api.github.test", **settings)
    return Updater(lambda: chosen, transport=httpx.MockTransport(handler))


async def test_check_finds_the_latest_release_and_records_failures(home: Path) -> None:
    wheel = make_wheel(home.parent / "dist", "0.3.0")
    seen: list[str] = []
    replies: list[httpx.Response] = [httpx.Response(200, json=release_json("0.3.0", wheel))]

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return replies[0]

    updater = updater_with(handler)
    assert updater.status()["latest"] is None and not updater.available
    release = await updater.check()
    assert seen == ["https://api.github.test/repos/JianShanAgent/agent-core/releases/latest"]
    assert release is not None and (release.version, release.tag) == ("0.3.0", "v0.3.0")
    assert release.asset_name == wheel.name and len(release.sha256) == 64
    status = updater.status()
    assert status["available"] and status["can_apply"] and status["blocked"] == ""
    assert (status["current"], status["installed"], status["overlay"]) == (BASE, BASE, False)
    assert status["latest"]["notes"].startswith("改了这些") and updater.brief() == {
        "available": True,
        "latest": "0.3.0",
    }

    # 查不成（限流、断网）：记下原因，上一次查到的留着
    replies[0] = httpx.Response(403, json={"message": "rate limit"})
    await updater.check()
    assert "403" in updater.error and updater.available

    # 仓库还没发过版本：不算出错
    replies[0] = httpx.Response(404, json={"message": "Not Found"})
    await updater.check()
    assert updater.error == "" and updater.latest is None and not updater.available

    # 最新的就是正在用的；标签不是版本号的认不出来
    replies[0] = httpx.Response(200, json=release_json(BASE))
    await updater.check()
    assert not updater.available and updater.status()["blocked"] == ""
    replies[0] = httpx.Response(200, json=release_json("x", tag_name="nightly"))
    await updater.check()
    assert "不是版本号" in updater.error


def test_a_release_without_a_package_or_checksum_cannot_be_applied(home: Path) -> None:
    updater = updater_with(lambda _r: httpx.Response(500))
    updater.latest = parse_release(release_json("0.3.0"))
    assert "没有带安装包" in updater.blocked() and not updater.status()["can_apply"]
    updater.latest = Release(
        version="0.3.0", tag="v0.3.0", name="", notes="", published_at="", url="",
        asset_name="x.whl", asset_url="https://github.test/x.whl",
    )  # fmt: skip
    assert "没有带校验和" in updater.blocked()


# ---------- 装 ----------


def test_a_good_package_is_unpacked_and_bad_ones_are_refused(home: Path) -> None:
    dist = home.parent / "dist"
    target = install_wheel(make_wheel(dist, "0.3.0"), "0.3.0", home)
    assert target == home / "versions" / "0.3.0"
    assert (target / "jianshanagent" / "__init__.py").read_text() == 'VERSION = "0.3.0"\n'
    assert sorted(p.name for p in (home / "versions").iterdir()) == ["0.3.0"]  # 没留下半成品

    def refused(match: str, version: str = "0.4.0", **kwargs: Any) -> None:
        with pytest.raises(UpdateError, match=match):
            install_wheel(make_wheel(dist / match[:6], version, **kwargs), "0.4.0", home)
        assert not (home / "versions" / "0.4.0").exists()
        assert not (home / "versions" / "0.4.0.tmp").exists()

    refused("安装包不是 jianshanagent 0.4.0", name="something-else")
    refused("不是纯 Python", tag="cp312-cp312-manylinux_2_28_x86_64")
    refused("要 Python >=3.99", python=">=3.99")
    refused("编译不过", files={"jianshanagent/broken.py": "def oops(:\n"})
    refused("不该有的文件", files={"evil/__init__.py": ""})
    refused("不该有的文件", files={"../outside.py": ""})
    # 依赖变了：更新包只换核心自己的代码，要重建环境
    refused("第三方依赖和环境里装的不一样（newdep>=1）", requires=[*REQUIRES, "newdep >= 1"])
    refused("httpx>=0.28", requires=["httpx>=0.28", "pyyaml>=6.0"])
    # 少了一个依赖、可选依赖变了：不碍事
    install_wheel(
        make_wheel(dist / "fewer", "0.4.0", requires=["httpx >= 0.27", "ruff; extra == 'dev'"]),
        "0.4.0",
        home,
    )
    not_a_zip = dist / "junk.whl"
    not_a_zip.write_bytes(b"not a zip")
    with pytest.raises(UpdateError, match="打不开"):
        install_wheel(not_a_zip, "0.5.0", home)


async def test_apply_downloads_verifies_and_marks_the_package_pending(home: Path) -> None:
    dist = home.parent / "dist"
    wheel = make_wheel(dist, "0.3.0")
    served = {"body": wheel.read_bytes(), "release": release_json("0.3.0", wheel)}
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(str(request.url))
        if request.url.host == "api.github.test":
            return httpx.Response(200, json=served["release"])
        return httpx.Response(200, content=served["body"])

    updater = updater_with(handler, download_prefix="https://mirror.test/")
    assert await updater.apply() == "0.3.0"
    # 下载走镜像，校验和取自接口
    assert paths[-1] == f"https://mirror.test/https://github.test/dl/{wheel.name}"
    state = _bootstrap.read_state(home)
    assert (state["current"], state["status"], state["attempts"]) == ("0.3.0", "pending", 0)
    assert state["previous"] == "" and not (home / "tmp").exists()
    assert updater.current == BASE  # 还没重启：正在用的没变

    # 重启后：机器人去试它，启动完成后确认，清掉用不着的
    _bootstrap._begin_trial()
    put(home, "0.1.5")  # 以前留下的旧版本
    updater.started()
    assert _bootstrap.read_state(home)["status"] == "ok" and updater.current == "0.3.0"
    assert sorted(p.name for p in (home / "versions").iterdir()) == ["0.3.0"]
    status = updater.status()
    assert (status["current"], status["installed"], status["overlay"]) == ("0.3.0", BASE, True)
    assert not status["available"]

    # 再出一个版本：上一个确认过的记成 previous
    newer = make_wheel(dist, "0.4.0")
    served.update(body=newer.read_bytes(), release=release_json("0.4.0", newer))
    assert await updater.apply() == "0.4.0"
    assert _bootstrap.read_state(home)["previous"] == "0.3.0"

    # 下载到的东西和校验和对不上（镜像给了别的文件）：不装
    tampered = make_wheel(dist, "0.5.0")
    served.update(body=b"something else", release=release_json("0.5.0", tampered))
    with pytest.raises(UpdateError, match="校验和对不上"):
        await updater.apply()
    assert _bootstrap.read_state(home)["current"] == "0.4.0"
    assert not (home / "versions" / "0.5.0").exists() and not updater.applying


async def test_why_a_package_cannot_be_used_stays_visible(home: Path) -> None:
    dist = home.parent / "dist"
    wheel = make_wheel(dist, "0.3.0", requires=[*REQUIRES, "newdep >= 1"])
    served = {"body": wheel.read_bytes(), "release": release_json("0.3.0", wheel)}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.github.test":
            return httpx.Response(200, json=served["release"])
        return httpx.Response(200, content=served["body"])

    updater = updater_with(handler)
    await updater.check()
    assert updater.status()["can_apply"]  # 依赖变没变要下载下来才知道
    with pytest.raises(UpdateError, match="第三方依赖和环境里装的不一样（newdep>=1）"):
        await updater.apply()
    # 原来只有点的那一下有一条提示，过后页面上又是一个能点的「更新」按钮
    status = updater.status()
    assert status["available"] and not status["can_apply"]
    assert "newdep>=1" in status["blocked"] and "docker compose build" in status["blocked"]
    assert not home.joinpath("state.json").exists() and not (home / "versions" / "0.3.0").exists()

    # 主仓库出了别的版本：这条结论不再挡着
    fine = make_wheel(dist, "0.3.1")
    served.update(body=fine.read_bytes(), release=release_json("0.3.1", fine))
    await updater.check()
    assert updater.status()["can_apply"] and updater.status()["blocked"] == ""

    # 下载没成、校验和对不上是一时的事：不记成「这个版本用不了」
    served.update(body=b"garbage")
    with pytest.raises(UpdateError, match="校验和对不上"):
        await updater.apply()
    assert updater.status()["can_apply"]


async def test_the_checksum_can_come_from_the_sums_file(home: Path) -> None:
    wheel = make_wheel(home.parent / "dist", "0.3.0")
    release = release_json("0.3.0", wheel)
    digest = release["assets"][0].pop("digest").removeprefix("sha256:")
    release["assets"].append(
        {"name": "SHA256SUMS", "browser_download_url": "https://github.test/dl/SHA256SUMS"}
    )
    sums = [f"{'0' * 64}  other.whl\n{digest}  {wheel.name}\n"]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.github.test":
            return httpx.Response(200, json=release)
        if request.url.path.endswith("SHA256SUMS"):
            return httpx.Response(200, text=sums[0])
        return httpx.Response(200, content=wheel.read_bytes())

    updater = updater_with(handler)
    assert await updater.apply() == "0.3.0"
    sums[0] = f"{'0' * 64}  other.whl\n"
    _bootstrap.write_state(home, {"current": "", "status": ""})
    with pytest.raises(UpdateError, match="校验和清单里没有"):
        await updater.apply()


async def test_nothing_is_applied_where_packages_are_not_used(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wheel = make_wheel(home.parent / "dist", "0.3.0")
    updater = updater_with(lambda _r: httpx.Response(200, json=release_json("0.3.0", wheel)))
    monkeypatch.setenv(_bootstrap.RELEASE_ENV, "7d4941f")
    await updater.check()
    status = updater.status()
    # 有新版本照样提示，只是不在程序里装
    assert status["available"] and not status["can_apply"] and status["mode"] == "managed"
    assert "发布流程" in status["blocked"]
    with pytest.raises(UpdateError, match="发布流程"):
        await updater.apply()
    with pytest.raises(UpdateError, match="发布流程"):
        updater.revert()
    assert not home.exists()

    monkeypatch.delenv(_bootstrap.RELEASE_ENV)
    with pytest.raises(UpdateError, match="现在用的就是环境里装的版本"):
        updater.revert()
    put(home, "0.3.0")
    _bootstrap.write_state(home, {"current": "0.3.0", "status": "ok"})
    assert updater.revert() == BASE and _bootstrap.confirmed_version(home) == ""
    updater.started()  # 重启后：更新包清掉
    assert list((home / "versions").iterdir()) == []


def test_settings_are_read_and_checked() -> None:
    from deployment import FULL_CONFIG_DIR
    from jianshanagent.core.config import is_locked, load_config

    config = load_config(FULL_CONFIG_DIR, "")
    settings = UpdateSettings.from_config(config)
    assert settings == UpdateSettings()  # 默认配置和代码里的默认值一致
    assert settings.repo == "JianShanAgent/agent-core"
    # 从哪更新不能在网页上改
    for key in ("repo", "api_url", "download_prefix"):
        assert is_locked("core.update", (key,))
    assert not is_locked("core.update", ("enabled",))
    config.files["core.update"] = {"repo": "https://evil.example/x", "check_interval_hours": 0.1}
    with pytest.raises(ValueError, match="<组织>/<仓库>"):
        UpdateSettings.from_config(config)
    config.files["core.update"] = {"check_interval_hours": 0.1, "enabled": False}
    lenient = UpdateSettings.from_config(config)
    assert lenient.check_interval_hours == 1.0 and lenient.enabled is False


# ---------- 管理后台的接口 ----------


async def test_the_admin_can_see_apply_and_revert_an_update(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil

    from deployment import FULL_CONFIG_DIR

    root = tmp_path / "config"
    shutil.copytree(FULL_CONFIG_DIR, root, ignore=shutil.ignore_patterns("local", "secrets.yaml"))
    (root / "secrets.yaml").write_text(
        f"llm:\n  api_key: tp-test\nadmin:\n  token: {TOKEN}\n", encoding="utf-8"
    )
    (root / "core.admin.yaml").write_text(
        (root / "core.admin.yaml")
        .read_text("utf-8")
        .replace("token_file: data/admin/token", f"token_file: {tmp_path / 'admin' / 'token'}"),
        encoding="utf-8",
    )
    monkeypatch.setenv("JIANSHANAGENT_CONFIG_DIR", str(root))
    monkeypatch.setenv("JIANSHANAGENT_CONFIG_OVERRIDES_DIR", str(tmp_path / "overrides"))
    monkeypatch.delenv("JIANSHANAGENT_POSTGRES_DSN", raising=False)
    monkeypatch.delenv("JIANSHANAGENT_LLM_API_KEY", raising=False)
    monkeypatch.setattr("jianshanagent.runtime.setup_logging", lambda **_kwargs: None)

    wheel = make_wheel(tmp_path / "dist", "0.3.0")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.github.com":
            return httpx.Response(200, json=release_json("0.3.0", wheel))
        return httpx.Response(200, content=wheel.read_bytes())

    runtime = Runtime(dry_run=True)
    runtime.updater._transport = httpx.MockTransport(handler)
    await runtime.prepare()
    stops: list[bool] = []
    monkeypatch.setattr(runtime, "request_stop", lambda *, restart=False: stops.append(restart))
    client = TestClient(TestServer(runtime.build_app()))
    await client.start_server()
    try:
        assert (await client.get("/api/update")).status == 401  # 要登录
        assert (await client.post("/api/login", json={"token": TOKEN})).status == 200
        assert (await (await client.get("/api/status")).json())["update"] == {
            "available": False,
            "latest": None,
        }
        bare = await client.post("/api/update/apply")  # 改东西的请求要带自定义头
        assert bare.status == 403

        checked = await (await client.post("/api/update/check", headers=WRITE)).json()
        assert (
            checked["available"]
            and checked["can_apply"]
            and checked["latest"]["version"] == "0.3.0"
        )
        assert (await (await client.get("/api/status")).json())["update"]["latest"] == "0.3.0"

        applied = await client.post("/api/update/apply", headers=WRITE)
        assert await applied.json() == {"restarting": True, "version": "0.3.0"}
        assert _bootstrap.read_state(home)["status"] == "pending"
        import asyncio

        await asyncio.sleep(0.7)  # 回应发出去以后才重启
        assert stops == [True]

        # 装不了的时候如实说，不重启
        monkeypatch.setenv(_bootstrap.RELEASE_ENV, "7d4941f")
        refused = await client.post("/api/update/apply", headers=WRITE)
        assert refused.status == 409 and "发布流程" in (await refused.json())["error"]
        monkeypatch.delenv(_bootstrap.RELEASE_ENV)

        _bootstrap.write_state(home, {"current": "0.3.0", "status": "ok"})
        reverted = await client.post("/api/update/revert", headers=WRITE)
        assert await reverted.json() == {"restarting": True, "version": BASE}
        again = await client.post("/api/update/revert", headers=WRITE)
        assert again.status == 409
    finally:
        await client.close()
        await runtime.stop()
