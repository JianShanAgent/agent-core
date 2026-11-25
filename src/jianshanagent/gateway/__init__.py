"""机器人消息接入，不负责业务路由。"""

from typing import TYPE_CHECKING, Any

from jianshanagent.gateway.base import Gateway, GatewayContext, GatewayFactory

if TYPE_CHECKING:
    from jianshanagent.gateway.qq import QQGateway

__all__ = ["Gateway", "GatewayContext", "GatewayFactory", "QQGateway"]


def __getattr__(name: str) -> Any:
    # 兼容旧的导入路径，同时不在 QQ 关闭时主动导入实现。
    if name == "QQGateway":
        from jianshanagent.gateway.qq import QQGateway

        return QQGateway
    raise AttributeError(name)
