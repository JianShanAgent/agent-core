from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ChatScope(StrEnum):
    C2C = "c2c"
    GROUP = "group"
    GUILD = "guild"
    DM = "dm"


CHANNEL_QQ = "qq"
CHANNEL_WEB = "web"


@dataclass(frozen=True, slots=True)
class ImageRef:
    """用户发来的一张图片：只有地址和平台给的说明，图片本身要用时再下载（core/media.py）。

    平台的附件地址有时效，不要长期保存。
    """

    url: str
    content_type: str = ""
    width: int = 0
    height: int = 0
    size: int = 0  # 字节数
    filename: str = ""
    #: 图片本身。管理后台的网页对话里上传的图片直接带着内容（这时 ``url`` 是空的），不用再下载
    data: bytes = field(default=b"", repr=False)


@dataclass(slots=True)
class InboundMessage:
    """网关无关的入站消息。"""

    message_id: str
    chat_scope: ChatScope
    chat_id: str
    user_id: str
    #: 文字部分；只发了图片的消息这里是空的
    content: str
    timestamp: str = ""
    user_name: str | None = None
    group_id: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    #: 消息里带的图片（语音、视频、文件不在这里）
    images: list[ImageRef] = field(default_factory=list)
    #: 要不要机器人处理。False = 只是路过的消息：群里没有 @ 机器人的发言（群主让机器人收全部消息的
    #: 群才有），或者没有文字也没有图片的消息。这种消息只给旁听的插件看一眼（比如记群聊记录），
    #: 不进命令和对话，也不回复
    addressed: bool = True
    #: 这条不是用户发的消息，而是他点了机器人消息上的按钮（``content`` 是按钮带的指令）时，
    #: 这里是平台那次点击事件的 id：回复要挂在它上面。这时 ``message_id`` 不是一条真消息，不能引用
    reply_event_id: str | None = None
    #: 从哪里来的：qq = QQ 平台；web = 管理后台的网页对话（调试用：处理的流程一样，
    #: 但不给旁听的插件看——不进群聊记录）
    channel: str = CHANNEL_QQ
    #: 网关给的「中途先发一句」的出口，见 :meth:`progress`；网关不支持时是 None
    progress_sink: ProgressSink | None = field(default=None, repr=False, compare=False)

    @property
    def session_key(self) -> str:
        """对话历史、记忆、限流都按它分开。

        一对一的会话（单聊、频道私信）就是会话本身；多人的会话（群、频道）要带上发言人——
        不然同一个群 / 子频道里所有人共用一份记忆和上文，一个人还能把别人的删掉。
        """
        if self.chat_scope in (ChatScope.C2C, ChatScope.DM):
            return f"{self.chat_scope}:{self.chat_id}"
        return f"{self.chat_scope}:{self.chat_id}:user:{self.user_id}"

    @property
    def user_suffix(self) -> str:
        """这个人在多人会话里的 ``session_key`` 都以它结尾（见上）；没有发言人标识时是空的。

        用户要求删除他的数据时，按它把他在别的群 / 频道里的也找出来一起删：
        平台在各个群里给同一个人的标识是同一个（线上的群聊记录里核对过），
        只删他提出要求的这一个群的话，别的群里的还留着。
        """
        return f":user:{self.user_id}" if self.user_id else ""

    async def progress(self, text: str) -> None:
        """处理要花好几秒时，中途给用户发一句简短的进度，免得对方以为卡住了。

        它是单独的一条纯文本消息，单聊、群聊一样；最终回复照常 return，和它无关。
        只有第一句进度和最终回复引用用户的那条消息；中间的进度引用机器人自己的上一句，
        免得每一句都给提问的人弹一次回复提醒。
        发不发得出去由网关决定：平台限制一条消息最多回复几次，网关给最终回复留着名额，
        名额不够时这里什么都不做。网关是在后台发的，这里不等它发完就返回；
        先报的先发，最终回复排在它们后面。
        """
        if self.progress_sink is not None:
            await self.progress_sink(OutboundMessage(content=text, reply_to=self.message_id))


@dataclass(frozen=True, slots=True)
class Button:
    """按钮：点一下，就当作提问的人发了 ``command``。

    平台直接把点击通知机器人（回调），不经过输入框；只有提问的那个人点了才算，别人点不了。
    """

    label: str
    command: str


@dataclass(slots=True)
class OutboundMessage:
    """出站回复。``markdown`` 为真时 content 是 Markdown；带按钮的消息一律按 Markdown 发。"""

    content: str
    reply_to: str | None = None
    markdown: bool = False
    buttons: list[Button] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


ProgressSink = Callable[[OutboundMessage], Awaitable[None]]


@dataclass(slots=True)
class ReplyBudget:
    """被动回复预算，由网关维护。"""

    message_id: str
    remaining: int
    expires_at_epoch: float
    msg_seq: int = 1
