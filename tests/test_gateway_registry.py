"""Gateway discovery, isolation from business reload, and startup rollback."""

import asyncio
from importlib.metadata import EntryPoint
from pathlib import Path
from typing import Any

import pytest
from aiohttp import ClientSession
from aiohttp.web import TCPSite

from deployment import FULL_CONFIG_DIR
from jianshanagent.core.config import AppConfig
from jianshanagent.core.messages import InboundMessage, OutboundMessage
from jianshanagent.gateway import GatewayContext
from jianshanagent.gateway import registry as loader
from jianshanagent.runtime import Runtime


class FakeGateway:
    def __init__(self, name: str, events: list[str], *, fail: bool = False):
        self.name, self.events, self.fail = name, events, fail

    async def start(self) -> None:
        self.events.append(f"start:{self.name}")
        if self.fail:
            raise RuntimeError("start error")

    async def stop(self) -> None:
        self.events.append(f"stop:{self.name}")

    def health(self) -> tuple[bool, str]:
        return not self.fail, "fake down" if self.fail else ""


def demo_factory(context: GatewayContext) -> FakeGateway:
    assert context.settings == {"enabled": True, "option": "demo"}
    assert context.secrets == {"key": "demo secret"}
    return FakeGateway("demo", [])


async def handle(message: InboundMessage) -> OutboundMessage | None:
    return None


async def sent(message: InboundMessage, outbound: OutboundMessage) -> None:
    pass


def _config(**gateways: dict[str, Any]) -> AppConfig:
    return AppConfig(
        Path("."),
        {f"gateway.{key}": value for key, value in {"qq": {"enabled": False}, **gateways}.items()},
        {"gateway": {"demo": {"key": "demo secret"}}},
    )


def test_installed_entry_points_are_only_imported_when_enabled(monkeypatch: pytest.MonkeyPatch):
    entries = [
        EntryPoint("demo", f"{__name__}:demo_factory", loader.ENTRY_POINT_GROUP),
        EntryPoint("off", "nonexistent_disabled_module:factory", loader.ENTRY_POINT_GROUP),
    ]

    def entry_points(*, group: str) -> list[EntryPoint]:
        assert group == loader.ENTRY_POINT_GROUP
        return entries

    monkeypatch.setattr(loader, "entry_points", entry_points)
    config = _config(demo={"enabled": True, "option": "demo"}, off={"enabled": False})
    found = loader.build_gateways(config, handle=handle, sent=sent, dry_run=False)
    assert list(found) == ["demo"] and isinstance(found["demo"], FakeGateway)
    assert loader.build_gateways(config, handle=handle, sent=sent, dry_run=True) == {}

    entries.append(entries[0])
    with pytest.raises(ValueError, match="ID 重复：demo"):
        loader.build_gateways(config, handle=handle, sent=sent, dry_run=False)
    with pytest.raises(ValueError, match="未安装：unknown"):
        loader.build_gateways(_config(unknown={"enabled": True}), handle=handle, sent=sent,
                              dry_run=False)


@pytest.fixture
async def runtime(monkeypatch: pytest.MonkeyPatch) -> Runtime:
    monkeypatch.setenv("JIANSHANAGENT_CONFIG_DIR", str(FULL_CONFIG_DIR))
    monkeypatch.setenv("JIANSHANAGENT_CONFIG_OVERRIDES_DIR", "")
    monkeypatch.delenv("JIANSHANAGENT_POSTGRES_DSN", raising=False)
    monkeypatch.setenv("JIANSHANAGENT_LLM_API_KEY", "test")
    runtime = Runtime(dry_run=True)
    await runtime.prepare()
    return runtime


async def test_gateway_lifecycle_and_reload(runtime: Runtime, monkeypatch: pytest.MonkeyPatch):
    # Serve the management app on a dynamically allocated local port, with two fake gateways.
    monkeypatch.setattr("jianshanagent.runtime.web.TCPSite", _ephemeral_site())
    events: list[str] = []
    first, second = FakeGateway("first", events), FakeGateway("second", events)
    runtime.gateways = {"first": first, "second": second}
    try:
        await runtime.serve()
        assert events == ["start:first", "start:second"]
        await runtime.reload()
        assert runtime.gateways["second"] is second and events == ["start:first", "start:second"]
        disk = AppConfig(
            Path("."), {**runtime.config.files, "gateway.demo": {"enabled": True}}, {}
        )
        assert runtime.pending_restart(disk) == ["gateway.demo"]
        assert runtime.pending_reload(disk) == []
        assert (await runtime._health(None)).status == 200  # type: ignore[arg-type]
        second.fail = True
        assert (await runtime._health(None)).status == 503  # type: ignore[arg-type]
    finally:
        await runtime.stop()
    assert events == ["start:first", "start:second", "stop:second", "stop:first"]


async def test_a_partially_started_gateway_is_stopped(runtime: Runtime, monkeypatch):
    monkeypatch.setattr("jianshanagent.runtime.web.TCPSite", _ephemeral_site())
    events: list[str] = []
    runtime.gateways = {
        "first": FakeGateway("first", events),
        "broken": FakeGateway("broken", events, fail=True),
    }
    try:
        with pytest.raises(RuntimeError, match="start error"):
            await runtime.serve()
        assert runtime._web is None
    finally:
        await runtime.stop()
    assert events == ["start:first", "start:broken", "stop:broken", "stop:first"]


async def test_a_slow_gateway_does_not_prevent_other_ingress_from_closing(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr("jianshanagent.runtime.web.TCPSite", _ephemeral_site())
    monkeypatch.setattr("jianshanagent.runtime._GATEWAY_SHUTDOWN_SECONDS", 0.01)
    events: list[str] = []
    cancelled = asyncio.Event()

    class SlowGateway(FakeGateway):
        async def stop(self) -> None:
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

    runtime.gateways = {
        "working": FakeGateway("working", events), "slow": SlowGateway("slow", events)
    }
    await runtime.serve()
    async with asyncio.timeout(1):
        await runtime.stop()
        await cancelled.wait()
    assert runtime._web is None and "stop:working" in events


async def test_http_uses_the_real_command_router(runtime: Runtime, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("jianshanagent.runtime.web.TCPSite", _ephemeral_site())
    token = "a" * 32
    config = AppConfig(
        Path("."),
        {"gateway.http": {"enabled": True, "port": 0, "host": "0.0.0.0"}},
        {"gateway": {"http": {"clients": {"partner": token}}}},
    )
    runtime.gateways = loader.build_gateways(
        config, handle=runtime.handle, sent=runtime.sent, dry_run=True
    )
    assert list(runtime.gateways) == ["http"]
    try:
        await runtime.serve()
        gateway: Any = runtime.gateways["http"]
        host, port = gateway._listener.sockets[0].getsockname()
        assert host == "127.0.0.1"
        async with ClientSession() as client, client.post(
            f"http://{host}:{port}/v1/chat",
            json={"user_id": "partner-user", "text": "/新话题"},
            headers={"Authorization": f"Bearer {token}"},
        ) as response:
            assert response.status == 200
            data = await response.text()
            assert all(f"event: {name}\n" in data for name in ("start", "reply", "done"))
        record = runtime.tracer.recent[-1]
        assert (record.channel, record.route, record.command) == ("http", "command", "新话题")
    finally:
        await runtime.stop()


def _ephemeral_site():
    def create(runner, **kwargs):
        return TCPSite(runner, host="127.0.0.1", port=0)

    return create
