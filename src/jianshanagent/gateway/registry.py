"""Discover enabled gateways without importing disabled plugin code."""

from __future__ import annotations

import re
from importlib.metadata import EntryPoint, entry_points
from typing import TYPE_CHECKING

from jianshanagent.gateway.base import Gateway, GatewayContext, MessageHandler, SentHandler

if TYPE_CHECKING:
    from jianshanagent.core.config import AppConfig
    from jianshanagent.core.integrations import IntegrationStore

ENTRY_POINT_GROUP = "jianshanagent.gateways"
PREFIX = "gateway."
_ID = re.compile(r"[a-z][a-z0-9_]*")
_BUILTINS = {
    name: EntryPoint(name, f"jianshanagent.gateway.{name}:create_gateway", ENTRY_POINT_GROUP)
    for name in ("qq", "http")
}


def build_gateways(
    config: AppConfig,
    *,
    handle: MessageHandler,
    sent: SentHandler,
    dry_run: bool,
    integrations: IntegrationStore | None = None,
) -> dict[str, Gateway]:
    """Validate enabled entries and construct them before any listeners are opened."""
    enabled: list[str] = []
    for name in ["gateway.qq", *sorted(config.files.keys() - {"gateway.qq"})]:
        if not name.startswith(PREFIX):
            continue
        gateway_id = name.removeprefix(PREFIX)
        settings = config.get(name)
        flag = settings.get("enabled", gateway_id == "qq")
        if not isinstance(flag, bool):
            raise ValueError(f"{name}.enabled 必须是布尔值")
        if not flag:
            continue
        if not _ID.fullmatch(gateway_id):
            raise ValueError(f"网关 ID 无效：{gateway_id}")
        if not dry_run or gateway_id == "http":
            enabled.append(gateway_id)

    candidates = {name: [entry] for name, entry in _BUILTINS.items()}
    if enabled and not dry_run:
        for entry in entry_points(group=ENTRY_POINT_GROUP):
            candidates.setdefault(entry.name, []).append(entry)

    gateways: dict[str, Gateway] = {}
    for gateway_id in enabled:
        options = candidates.get(gateway_id, [])
        if not options:
            raise ValueError(f"启用的网关未安装：{gateway_id}")
        if len(options) != 1:
            raise ValueError(f"网关 ID 重复：{gateway_id}")
        settings = config.get(PREFIX + gateway_id)
        if dry_run:
            settings["host"] = "127.0.0.1"
        if gateway_id == "qq":
            app_id, secret = config.qq_credentials
            secrets = {"app_id": app_id, "client_secret": secret}
        else:
            all_gateway_secrets = config.secrets.get("gateway", {})
            if not isinstance(all_gateway_secrets, dict):
                raise ValueError("secrets.yaml 的 gateway 必须是键值结构")
            secrets = all_gateway_secrets.get(gateway_id, {})
            if not isinstance(secrets, dict):
                raise ValueError(f"网关 {gateway_id} 的凭据必须是键值结构")
        factory = options[0].load()
        context = GatewayContext(
            settings,
            secrets,
            handle,
            sent,
            integrations=integrations if gateway_id == "http" else None,
        )
        gateway = factory(context)
        if not all(callable(getattr(gateway, name, None)) for name in ("start", "stop", "health")):
            raise TypeError(f"网关 {gateway_id} 必须实现 start、stop 和 health")
        gateways[gateway_id] = gateway
    return gateways
