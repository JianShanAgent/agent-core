"""进程启动时那几项「要重启才生效」的设置：读出来对不对、写错了报不报、不连 QQ 的开关管不管用。"""

import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

import jianshanagent.runtime as runtime_module
from deployment import FULL_CONFIG_DIR
from jianshanagent.core.config import load_config
from jianshanagent.runtime import Runtime, _Boot

REPO_CONFIG = FULL_CONFIG_DIR


def _config_dir(tmp_path: Path, **edits: dict[str, Any]) -> Path:
    """仓库配置的拷贝（不带凭据和本机覆写），``edits`` 里的文件按给的内容改几项。"""
    root = tmp_path / "config"
    shutil.copytree(REPO_CONFIG, root, ignore=shutil.ignore_patterns("secrets.yaml", "local"))
    for name, changes in edits.items():
        path = root / f"{name.replace('__', '.')}.yaml"
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        for key, value in changes.items():
            node = data
            *parents, last = key.split(".")
            for parent in parents:
                node = node[parent]
            node[last] = value
        path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return root


def test_boot_settings_follow_the_repo_config(tmp_path: Path) -> None:
    boot = _Boot.from_config(load_config(_config_dir(tmp_path), with_overrides=False))
    assert boot.gateway_enabled is True
    assert (boot.pool_min, boot.pool_max) == (1, 10)
    assert boot.max_replies == {"c2c": 4, "group": 5}


def test_pool_sizes_and_the_qq_switch_are_read_and_checked(tmp_path: Path) -> None:
    root = _config_dir(
        tmp_path,
        gateway__qq={"enabled": False},
        core__storage={"postgres.pool_min": 2, "postgres.pool_max": 4},
    )
    boot = _Boot.from_config(load_config(root, with_overrides=False))
    assert boot.gateway_enabled is False and (boot.pool_min, boot.pool_max) == (2, 4)

    upside_down = _config_dir(
        tmp_path / "bad", core__storage={"postgres.pool_min": 5, "postgres.pool_max": 2}
    )
    with pytest.raises(ValueError, match="连接池大小不对"):
        _Boot.from_config(load_config(upside_down, with_overrides=False))


async def test_with_the_qq_switch_off_the_process_serves_the_admin_but_does_not_connect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _config_dir(
        tmp_path,
        gateway__qq={"enabled": False},
        app={"healthcheck.port": 18999, "healthcheck.host": "127.0.0.1"},
        core__admin={"enabled": False},
    )
    (root / "secrets.yaml").write_text("llm:\n  api_key: tp-test\n", encoding="utf-8")
    monkeypatch.setenv("JIANSHANAGENT_CONFIG_DIR", str(root))
    monkeypatch.delenv("JIANSHANAGENT_CONFIG_OVERRIDES_DIR", raising=False)
    monkeypatch.delenv("JIANSHANAGENT_POSTGRES_DSN", raising=False)
    monkeypatch.delenv("JIANSHANAGENT_LLM_API_KEY", raising=False)
    monkeypatch.setattr(runtime_module, "setup_logging", lambda **_kwargs: None)
    made: list[Any] = []

    class NoGateway:
        def __init__(self, **kwargs: Any) -> None:
            made.append(kwargs)  # 走到这里就是要去连 QQ 了

    monkeypatch.setattr(runtime_module, "QQGateway", NoGateway)
    runtime = Runtime(dry_run=False)  # 不是调试模式：开关是唯一拦着它的东西
    await runtime.prepare()
    try:
        await runtime.serve()
        assert made == [] and runtime.gateway is None
        response = await runtime._health(None)  # type: ignore[arg-type]
        assert response.status == 200  # 没有连接要看，健康检查照常是好的
    finally:
        await runtime.stop()
