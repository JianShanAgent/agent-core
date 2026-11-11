"""JianShanAgent（见山）：给 MediaWiki 站点用的 QQ 问答机器人。"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("jianshanagent")
except PackageNotFoundError:
    __version__ = "0.1.0.dev"

__all__ = ["__version__"]
