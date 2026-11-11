"""给模型看的「现在是什么时候」。

模型自己不知道今天是哪一天（会按训练时的年份去理解「今年」「这次活动」），
所以凡是可能涉及时间的提示词都要带上这一句。
"""

from __future__ import annotations

from datetime import datetime, tzinfo

_WEEKDAYS = "一二三四五六日"


def now_text(timezone: tzinfo | None = None) -> str:
    """例如「2026-11-11 星期三 10:05」。``timezone`` 不给就用机器的本地时区。"""
    now = datetime.now(timezone)
    return f"{now:%Y-%m-%d} 星期{_WEEKDAYS[now.weekday()]} {now:%H:%M}"
