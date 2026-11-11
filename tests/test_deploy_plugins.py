"""部署插件（``deploy/plugins/<id>/``）：和机器人一起部署、但机器人离了它照样跑的服务。

- 自带的部署插件合规范：清单说的服务和 Compose 片段里的一致，片段引用的文件都在；
- 主 Compose 文件里只剩机器人离不开的服务，日志那几个不在里面；
- 服务器上的 compose 入口（``deploy/scripts/ctl``）按 ``.env`` 的名单把片段合进去：
  名单没写、写错了都报错停下，不悄悄少起几个服务。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from jianshanagent.core.manifest import (
    COMPOSE_FILE,
    INIT_FILE,
    KIND_DEPLOY,
    PluginError,
    deploy_manifests,
    read_manifest,
)

REPO = Path(__file__).resolve().parents[1]
DEPLOY = REPO / "deploy"
PLUGINS = DEPLOY / "plugins"
CORE_SERVICES = {"qqbot", "kbsync", "postgres", "backup", "redis"}
RELEASE = "abc1234"


def compose(path: Path) -> dict[str, Any]:
    content = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(content, dict)
    return content


# ---------- 自带的部署插件合规范 ----------


def test_shipped_deploy_plugins_match_their_compose_fragments() -> None:
    plugins = deploy_manifests(PLUGINS)
    assert list(plugins) == ["logs"]
    logs = plugins["logs"]
    assert (logs.kind, logs.name) == (KIND_DEPLOY, "运行日志")
    assert logs.services == ("loki", "alloy", "grafana")
    assert logs.description

    base = compose(DEPLOY / "docker-compose.yml")
    # 主文件里只有机器人离不开的；部署插件带来的服务不和它们重名（合在一起时后写的会盖掉先写的）
    assert set(base["services"]) == CORE_SERVICES
    for manifest in plugins.values():
        fragment = compose(manifest.compose_path)
        assert not set(fragment["services"]) & CORE_SERVICES
        # 片段不自己定义网络，用主文件里的
        assert "networks" not in fragment
        for name, service in fragment["services"].items():
            assert set(service["networks"]) <= set(base["networks"]), name
            # 挂进容器的配置文件：路径从 deploy/ 算起，指的文件要在这个插件自己的目录里
            for volume in service.get("volumes", []):
                source = volume.split(":")[0] if isinstance(volume, str) else ""
                if source.startswith("./"):
                    assert source.startswith(f"./plugins/{manifest.id}/"), (name, source)
                    assert (DEPLOY / source).exists(), (name, source)
        # 上线前的准备脚本可以没有；有的话要能直接执行
        init = manifest.path / INIT_FILE
        assert not init.exists() or os.access(init, os.X_OK)


def test_the_bot_does_not_depend_on_any_deploy_plugin() -> None:
    base = compose(DEPLOY / "docker-compose.yml")
    for name, service in base["services"].items():
        needs = service.get("depends_on") or {}
        assert set(needs) <= CORE_SERVICES, name


def write_plugin(root: Path, plugin_id: str, manifest: dict[str, Any], services: list[str]) -> Path:
    directory = root / plugin_id
    directory.mkdir(parents=True)
    (directory / "plugin.yaml").write_text(yaml.safe_dump(manifest, allow_unicode=True), "utf-8")
    fragment = {"services": {name: {"image": "busybox"} for name in services}}
    (directory / COMPOSE_FILE).write_text(yaml.safe_dump(fragment), "utf-8")
    return directory


def test_a_malformed_deploy_plugin_says_what_is_wrong(tmp_path: Path) -> None:
    good = {"id": "metrics", "kind": "deploy", "name": "指标", "services": ["prometheus"]}
    directory = write_plugin(tmp_path / "ok", "metrics", good, ["prometheus"])
    assert read_manifest(directory, "", KIND_DEPLOY).services == ("prometheus",)

    # 清单说的服务和片段里的对不上
    directory = write_plugin(tmp_path / "drift", "metrics", good, ["prometheus", "exporter"])
    with pytest.raises(PluginError, match="对不上"):
        read_manifest(directory, "", KIND_DEPLOY)
    # 没写带来哪些服务
    directory = write_plugin(
        tmp_path / "empty", "metrics", {k: v for k, v in good.items() if k != "services"}, ["x"]
    )
    with pytest.raises(PluginError, match="没有写 services"):
        read_manifest(directory, "", KIND_DEPLOY)
    # 没有 Compose 片段
    directory = write_plugin(tmp_path / "bare", "metrics", good, ["prometheus"])
    (directory / COMPOSE_FILE).unlink()
    with pytest.raises(PluginError, match="没有 Compose 片段"):
        read_manifest(directory, "", KIND_DEPLOY)
    # 别的种类的键不认
    directory = write_plugin(
        tmp_path / "keys", "metrics", {**good, "capabilities": ["wiki"]}, ["prometheus"]
    )
    with pytest.raises(PluginError, match="不认识的项"):
        read_manifest(directory, "", KIND_DEPLOY)
    # 部署插件的参数是 Compose 变量，没有配置文件
    with pytest.raises(PluginError, match="没有配置文件"):
        _ = deploy_manifests(PLUGINS)["logs"].config_name

    # 两个插件带了同名的服务
    both = tmp_path / "both"
    write_plugin(both, "metrics", good, ["prometheus"])
    write_plugin(both, "other", {**good, "id": "other"}, ["prometheus"])
    with pytest.raises(PluginError, match="都带了叫 prometheus 的服务"):
        deploy_manifests(both)


# ---------- 服务器上的 compose 入口 ----------


@pytest.fixture
def server(tmp_path: Path) -> Path:
    """一个假的服务器根目录：一个发布（内容是仓库里的 deploy/）、current 指向它，docker 是假的。"""
    root = tmp_path / "root"
    shutil.copytree(DEPLOY, root / "releases" / RELEASE / "deploy")
    (root / "shared").mkdir()
    (root / "current").symlink_to(Path("releases") / RELEASE)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "docker"
    # 把收到的参数一行一个打出来，连同 ctl 给它的发布号
    fake.write_text(
        '#!/bin/sh\necho "release=$JIANSHANAGENT_RELEASE"\nprintf \'%s\\n\' "$@"\n', "utf-8"
    )
    fake.chmod(0o755)
    return root


def ctl(root: Path, env_file: str | None, *args: str) -> subprocess.CompletedProcess[str]:
    if env_file is not None:
        (root / "shared" / ".env").write_text(env_file, "utf-8")
    env = {
        "PATH": f"{root.parent / 'bin'}:{os.environ['PATH']}",
        "JIANSHANAGENT_ROOT": str(root),
    }
    script = root / "releases" / RELEASE / "deploy" / "scripts" / "ctl"
    return subprocess.run(
        ["sh", str(script), *args], env=env, capture_output=True, text=True, check=False
    )


def compose_files(result: subprocess.CompletedProcess[str], root: Path) -> list[str]:
    """假 docker 收到的参数里 -f 后面跟的文件，写成相对于发布里 deploy/ 的路径。"""
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    deploy = (root / "releases" / RELEASE / "deploy").resolve()
    files = [lines[i + 1] for i, line in enumerate(lines) if line == "-f"]
    return [str(Path(name).resolve().relative_to(deploy)) for name in files]


def test_ctl_adds_the_fragments_of_the_listed_deploy_plugins(server: Path) -> None:
    result = ctl(server, "POSTGRES_PASSWORD=x\nJIANSHANAGENT_DEPLOY_PLUGINS=logs\n", "ps", "--all")
    assert compose_files(result, server) == ["docker-compose.yml", "plugins/logs/compose.yaml"]
    lines = result.stdout.splitlines()
    assert lines[0] == f"release={RELEASE}"
    # 子命令和它的参数原样排在最后，-f 都在它前面
    assert lines[-2:] == ["ps", "--all"]
    assert "--env-file" in lines and lines[1] == "compose"

    # 一个都不带：只有主文件
    result = ctl(server, "JIANSHANAGENT_DEPLOY_PLUGINS=\n", "ps")
    assert compose_files(result, server) == ["docker-compose.yml"]

    # 逗号、空格、引号都行；同一个变量写了两行时后面的算数（和 compose 读 .env 一样）
    (server / "releases" / RELEASE / "deploy" / "plugins" / "metrics").mkdir()
    fragment = server / "releases" / RELEASE / "deploy" / "plugins" / "metrics" / COMPOSE_FILE
    fragment.write_text("services: {}\n", "utf-8")
    result = ctl(
        server,
        'JIANSHANAGENT_DEPLOY_PLUGINS=\nJIANSHANAGENT_DEPLOY_PLUGINS="logs, metrics"\n',
        "ps",
    )
    assert sorted(compose_files(result, server)) == [
        "docker-compose.yml", "plugins/logs/compose.yaml", "plugins/metrics/compose.yaml",
    ]  # fmt: skip


def test_ctl_stops_instead_of_quietly_dropping_services(server: Path) -> None:
    # .env 里没有这一行：不当成「一个都不带」，直接报错
    result = ctl(server, "POSTGRES_PASSWORD=x\n", "up", "-d")
    assert result.returncode == 1 and "没有 JIANSHANAGENT_DEPLOY_PLUGINS" in result.stderr
    assert result.stdout == ""  # docker 没有被调用

    # 列了这个发布里没有的插件
    result = ctl(server, "JIANSHANAGENT_DEPLOY_PLUGINS=logs,metrics\n", "up", "-d")
    assert result.returncode == 1 and "没有部署插件「metrics」" in result.stderr
    assert result.stdout == ""

    # 名字里有不该有的东西（路径之类）
    result = ctl(server, "JIANSHANAGENT_DEPLOY_PLUGINS=../../etc\n", "up", "-d")
    assert result.returncode == 1 and "名字不对" in result.stderr

    # 没有 .env
    (server / "shared" / ".env").unlink()
    result = ctl(server, None, "ps")
    assert result.returncode == 1 and "server-init.sh" in result.stderr


def test_ctl_can_address_a_release_that_is_not_current(server: Path) -> None:
    (server / "current").unlink()
    (server / "shared" / ".env").write_text("JIANSHANAGENT_DEPLOY_PLUGINS=logs\n", "utf-8")
    env = {
        "PATH": f"{server.parent / 'bin'}:{os.environ['PATH']}",
        "JIANSHANAGENT_ROOT": str(server),
        "CTL_RELEASE": RELEASE,
    }
    script = server / "releases" / RELEASE / "deploy" / "scripts" / "ctl"
    result = subprocess.run(
        ["sh", str(script), "config", "-q"], env=env, capture_output=True, text=True, check=False
    )
    assert compose_files(result, server) == ["docker-compose.yml", "plugins/logs/compose.yaml"]
