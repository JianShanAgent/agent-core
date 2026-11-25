"""Stable contract for trusted, installed gateway plugins."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from jianshanagent.core.messages import InboundMessage, OutboundMessage

if TYPE_CHECKING:
    from jianshanagent.core.integrations import IntegrationStore

MessageHandler = Callable[[InboundMessage], Awaitable[OutboundMessage | None]]
SentHandler = Callable[[InboundMessage, OutboundMessage], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class GatewayContext:
    """A factory's own configuration and the shared message entry points."""

    settings: Mapping[str, Any]
    secrets: Mapping[str, Any] = field(repr=False)
    handle: MessageHandler
    sent: SentHandler
    integrations: IntegrationStore | None = None


class Gateway(Protocol):
    """Factories must not start I/O. Stop may be called after a partial start."""

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    def health(self) -> tuple[bool, str]: ...


GatewayFactory = Callable[[GatewayContext], Gateway]
