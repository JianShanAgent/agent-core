"""JianShanAgent（见山）：给 MediaWiki 站点用的 QQ 问答机器人。"""

# 先导入引导模块，再让它决定包的其余部分从哪来：数据目录里有确认过的更新包就用更新包
# （见 _bootstrap.py）。这两行要在导入包里任何别的模块之前。
from jianshanagent import _bootstrap

_bootstrap.activate()

#: 这个进程实际在用的版本（用了更新包时是更新包的版本）
__version__ = _bootstrap.running_version()

__all__ = ["__version__"]
