from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from jianshanagent.core.links import strip_unknown_links, urls_in
from jianshanagent.core.logging import get_logger
from jianshanagent.core.messages import Button, ChatScope, ImageRef, InboundMessage, OutboundMessage

if TYPE_CHECKING:
    from qqbot_agent_sdk.dto import MessageAttachment

logger = get_logger(__name__)

MessageHandler = Callable[[InboundMessage], Awaitable[OutboundMessage | None]]
#: 最终回复发出去以后通知一声：（回复的是哪条消息，发出去的内容）
SentHandler = Callable[[InboundMessage, OutboundMessage], Awaitable[None]]

_SEND_ATTEMPTS = 3  # 发送遇到一时的故障（超时、5xx）最多试几次
_QUOTE_GIVE_UP = 2  # 同一种引用连着被平台拒绝这么多次，就不再试了
_BUTTON_LABEL_CHARS = 10  # 平台限制：按钮文字最多 10 个字符
_BUTTONS_PER_ROW = 2
#: 按钮的样子（平台文档：0 灰色线框，1 蓝色线框，3 白色背景 + 红色字体，4 蓝色背景 + 白色字体）
BUTTON_STYLE = 3
#: 按钮点了以后做什么（平台文档：0 跳转，1 回调——平台把点击通知机器人，
#: 2 指令——替用户把字填进输入框）
_BUTTON_CALLBACK = 1
#: 谁能点（平台文档：0 指定的人，1 管理员，2 所有人）
_BUTTON_FOR_LISTED_USERS = 0
#: 点击事件里，「点了消息上的按钮」这一种的类型号（别的是机器人配置页面的事件，不管）
_INTERACTION_BUTTON = 11
#: 点击事件最外层的 id 放进事件内容时用的键（回复要挂在这个 id 上，SDK 不把它传给回调）
_ENVELOPE_ID = "_envelope_id"
#: SDK 放弃重连时报的原因里，过一阵重新连一条还可能连得上的那几种（连不上太多次、连上就断）。
#: 别的原因是平台不让连（下架、封禁、没有权限），重连没有用，要人去处理
_FATAL_RETRYABLE = frozenset({"qq_max_reconnect", "qq_quick_disconnect"})
#: 进程退出时，告诉还没处理完的那几条消息「在重启」最多花这么久
_NOTICE_SECONDS = 3.0
_RESTART_NOTICE = "机器人正在重启，这个问题没来得及处理完，请过一会儿再问一次。"
_LINKS_DROPPED = "（链接发不出去，已省略）"
#: SDK 报「平台返回了错误」时的写法，方括号里是 HTTP 状态码
_API_ERROR_STATUS = re.compile(r"QQ Bot API error \[(\d{3})\]")


def refused_by_platform(exc: BaseException) -> bool:
    """平台明说了不收这个请求（4xx），而不是超时、网络错误、平台自己出故障（5xx）。"""
    found = _API_ERROR_STATUS.search(str(exc))
    return found is not None and 400 <= int(found.group(1)) < 500


def guarded(name: str, callback: Callable[[str, dict[str, Any]], Awaitable[None]]) -> Any:
    """给事件回调兜底：SDK 把回调丢到主循环以后不看结果，里面抛了异常就一点痕迹都没有。"""

    async def run(event_type: str, raw: dict[str, Any]) -> None:
        try:
            await callback(event_type, raw)
        except Exception:
            logger.exception(name, type=event_type)

    return run


_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\((https?://[^)\s]+)\)")
_IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")

#: 群消息的两种事件。默认只有 @ 机器人的消息会推过来，用前一种；群主在手机 QQ 里把
#: 「机器人可获取的群聊消息范围」设成「获取群内全部消息」之后，这个群的每一条消息（包括 @ 机器人的）
#: 都改用后一种推。两种事件的字段完全一样（官方文档《群消息（全量模式）》）。SDK 只认前一种
GROUP_AT_EVENT = "GROUP_AT_MESSAGE_CREATE"
GROUP_FULL_EVENT = "GROUP_MESSAGE_CREATE"
_MENTION_RE = re.compile(r"<@!?([^>\s]+)>")  # 全部消息的事件里，@ 某个成员写成 <@成员标识>
_SHAPE_LOGS = 5  # 「有人被 @ 但认不出是不是机器人」的消息，每次启动最多记几条字段名，用来排查


def mention_view(raw: Mapping[str, Any]) -> tuple[bool, str]:
    """全部消息的群事件：这条是不是 @ 了机器人，以及把 @ 标记整理过的文字。

    被 @ 的人列在 ``mentions`` 里，@ 的是机器人自己的那一项带 ``is_you``。只认这个标记：
    认不出来宁可不回，也不能把群里的闲聊都当成对机器人说的。
    文字里 @ 机器人的标记去掉（和只推 @ 消息时平台给的文字一样），@ 别人的换成「@昵称」。
    """
    mentions = [m for m in raw.get("mentions") or [] if isinstance(m, Mapping)]
    me: set[str] = set()
    names: dict[str, str] = {}
    for mention in mentions:
        ids = {str(mention[key]) for key in ("id", "member_openid") if mention.get(key)}
        if mention.get("is_you") is True:
            me |= ids
        for one in ids:
            names[one] = str(mention.get("username") or "")

    def show(match: re.Match[str]) -> str:
        who = match.group(1)
        if who in me:
            return ""
        if who == "all":
            return "@全体成员"
        return f"@{names.get(who) or '某人'}"

    addressed = any(m.get("is_you") is True for m in mentions)
    return addressed, _MENTION_RE.sub(show, str(raw.get("content") or "")).strip()


def socket_class(base: Any) -> Any:
    """在 SDK 的连接类上补三件事：认全部消息的群事件；按钮点击事件带上最外层的事件 id；
    别的不认识的事件记一笔日志。

    SDK 对不认识的事件只在 debug 级别提一句就丢掉——群消息换了事件之后机器人不回复，
    日志里一点痕迹都没有。回复按钮点击要用事件最外层的 id（不是事件内容里的 id），
    SDK 只把事件内容传给回调，所以在这里记下来、塞进内容里一起传。
    这里用到 SDK 内部的四个方法（``_dispatch_payload`` / ``_handle_dispatch`` /
    ``_is_duplicate`` / ``_dispatch_to_main``），升级 SDK 时测试会检查它们还在不在。
    """
    from qqbot_agent_sdk.dto import INTERACTION_EVENT_TYPES, MESSAGE_EVENT_TYPES, EventType

    known = {
        str(kind.value)
        for kind in (EventType.READY, EventType.RESUMED)
        + tuple(MESSAGE_EVENT_TYPES)
        + tuple(INTERACTION_EVENT_TYPES)
    }

    interactions = {str(kind.value) for kind in INTERACTION_EVENT_TYPES}

    class Socket(base):  # type: ignore[misc]
        _envelope_id = ""  # 正在处理的这个事件最外层的 id（事件是一个一个按顺序处理的）

        def _dispatch_payload(self, raw: dict[str, Any]) -> None:
            self._envelope_id = str(raw.get("id") or "")
            super()._dispatch_payload(raw)

        def _handle_dispatch(self, event_type: str, data: dict[str, Any]) -> None:
            if event_type == GROUP_FULL_EVENT:
                message_id = str(data.get("id", ""))
                if message_id and self._is_duplicate(message_id):
                    return
                self._dispatch_to_main(self._cb.on_message_event(event_type, data))
                return
            if event_type in interactions and self._cb.on_interaction_event is not None:
                click_id = str(data.get("id", ""))
                if click_id and self._is_duplicate(f"interaction:{click_id}"):
                    return  # 断线续接时平台会把事件再发一遍
                event = {**data, _ENVELOPE_ID: self._envelope_id}
                self._dispatch_to_main(self._cb.on_interaction_event(event_type, event))
                return
            if event_type not in known:
                group = str(data.get("group_openid") or "")[:6] if isinstance(data, dict) else ""
                logger.info("qq_event_ignored", type=event_type, group=group or None)
            super()._handle_dispatch(event_type, data)

    return Socket


def image_refs(attachments: Iterable[MessageAttachment]) -> list[ImageRef]:
    """从平台给的附件里挑出图片；语音、视频、文件不要。

    附件写了类型就看是不是 ``image/`` 开头；没写类型的按文件扩展名判断。
    """
    refs: list[ImageRef] = []
    for item in attachments:
        kind = item.content_type.strip().lower()
        if kind:
            is_image = kind.startswith("image/")
        else:
            is_image = item.filename.strip().lower().endswith(_IMAGE_SUFFIXES)
        url = item.resolved_url  # 平台有时给 // 开头的地址，这里已经补成 https
        if is_image and url:
            refs.append(
                ImageRef(
                    url=url,
                    content_type=kind,
                    width=item.width,
                    height=item.height,
                    size=item.size,
                    filename=item.filename,
                )
            )
    return refs


def sent_ref(response: Mapping[str, Any]) -> str | None:
    """平台回给一条已发消息的「引用索引」：以后要引用这条消息时填它。没拿到返回 None。

    按文档，机器人自己发的消息用响应里的 ``ext_info.ref_idx``；响应不一定带它，没有就退回消息 id。
    """
    info = response.get("ext_info")
    ref = info.get("ref_idx") if isinstance(info, Mapping) else None
    return str(ref or response.get("id") or "") or None


def build_keyboard(buttons: Sequence[Button], *, user_id: str) -> dict[str, Any]:
    """回调按钮：点一下，平台直接把按钮带的指令通知机器人（不经过输入框）。

    只有 ``user_id``（提问的那个人）能点；白底红字。
    """

    def one(index: int, button: Button) -> dict[str, Any]:
        label = button.label
        if len(label) > _BUTTON_LABEL_CHARS:
            label = label[: _BUTTON_LABEL_CHARS - 1] + "…"
        return {
            "id": str(index),
            "render_data": {"label": label, "visited_label": label, "style": BUTTON_STYLE},
            "action": {
                "type": _BUTTON_CALLBACK,
                "permission": {"type": _BUTTON_FOR_LISTED_USERS, "specify_user_ids": [user_id]},
                "data": button.command,
                "unsupport_tips": "当前版本不支持按钮，请直接发送指令",
            },
        }

    items = [one(i, b) for i, b in enumerate(buttons)]
    rows = [
        {"buttons": items[i : i + _BUTTONS_PER_ROW]} for i in range(0, len(items), _BUTTONS_PER_ROW)
    ]
    return {"content": {"rows": rows}}


def markdown_to_text(content: str) -> str:
    """Markdown 发送失败时的纯文本版本：去掉加粗和引用标记，链接还原成「文字 网址」。"""
    text = _MD_LINK_RE.sub(lambda m: f"{m.group(1)} {m.group(2)}".strip(), content)
    text = text.replace("**", "")
    return "\n".join(line.removeprefix("> ") for line in text.splitlines())


class QQGateway:
    """qqbot-agent-sdk 薄适配：收事件 → InboundMessage；OutboundMessage → 发送。

    消息里的图片只取地址（``InboundMessage.images``），要用的一方自己下载；
    语音、视频、文件这些附件不处理。

    群里默认只收得到 @ 机器人的消息；群主让机器人收全部消息的群，每一条发言都会过来（见
    ``GROUP_FULL_EVENT``）。没 @ 机器人的发言、文字和图片都没有的消息，标成 ``addressed=False``
    交给处理方（只有旁听的插件会看，比如记群聊记录），网关不回复它们。

    处理方可以在中途发进度消息（``InboundMessage.progress``），单聊、群聊都是单独的一条短消息。
    平台限制一条消息最多被动回复几次（``max_replies``），最后一次名额留给最终回复，
    所以进度消息只在还有富余名额时才发，多出来的不发。
    进度是在后台发的，不让处理方等（平台处理一条消息要一两秒）：同一条消息的进度一句发完
    再发下一句，最终回复等它们都发完再发。

    引用谁（引用提问的人的消息，对方会收到一次「有人回复你」的提醒）：
    第一句进度和最终回复引用用户的那条消息——开始处理时告诉对方一声，出结果时再告诉一声。
    中间的进度不引用用户的消息，每一句引用机器人自己发的上一句，串成一串，过程中不反复打扰提问的人。
    平台不收某种引用时去掉引用照发，见 :meth:`send`。
    """

    def __init__(
        self,
        *,
        app_id: str,
        client_secret: str,
        on_message: MessageHandler,
        on_sent: SentHandler | None = None,
        session_store_dir: str | None = None,
        max_replies: Mapping[str, int] | None = None,
        retry_base_seconds: float = 1.0,
        drain_seconds: float = 20.0,
        watch_seconds: float = 15.0,
        revive_delays: Sequence[float] = (5.0, 15.0, 30.0, 60.0),
        unhealthy_after_seconds: float = 120.0,
    ):
        if not app_id or not client_secret:
            raise ValueError("缺少 QQ AppID / ClientSecret（见 config/secrets.yaml）")
        self.app_id = app_id
        self.client_secret = client_secret
        self.on_message = on_message
        #: 最终回复发出去以后通知谁（记群聊记录用）；进度消息不通知
        self.on_sent = on_sent
        self.session_store_dir = session_store_dir
        #: 一条消息最多能被动回复几次，按会话类型（c2c / group）；没写的类型不发进度消息
        self.max_replies = dict(max_replies or {})
        self.retry_base_seconds = retry_base_seconds
        #: 进程退出时，正在处理的消息最多等这么久；到点还没完的回一句「在重启」
        self.drain_seconds = drain_seconds
        #: 隔多久看一眼连接线程还在不在；不在了隔多久重新连一条（一次比一次久，用到最后一个为止）
        self.watch_seconds = watch_seconds
        self.revive_delays = tuple(revive_delays) or (60.0,)
        #: 连续这么久没连着，健康检查就报不正常（断线重连、每小时一次的续接只有几秒，不算）
        self.unhealthy_after_seconds = unhealthy_after_seconds
        #: 带引用的消息被平台连着拒绝了几次，按（引用的是不是机器人自己的消息，是不是 Markdown）记
        self._quote_rejections: dict[tuple[bool, bool], int] = {}
        self._shape_logs = _SHAPE_LOGS
        #: 连接的现状，给管理后台看：现在连没连着、什么时候连上的、断过几次、收到过几个事件
        self.connected = False
        self.connected_at: float | None = None
        self.disconnects = 0
        self.events = 0
        self.last_event_at: float | None = None
        #: 从什么时候起没连着；连着的时候是 None
        self.down_since: float | None = None
        #: SDK 放弃重连时报的原因；空 = 没有这回事
        self.fatal = ""
        #: 正在处理的消息（进程退出时等它们处理完）
        self._inflight: set[asyncio.Task[Any]] = set()
        self._stopping = False
        self._connect: Callable[[], Awaitable[None]] | None = None
        self._watchdog: asyncio.Task[None] | None = None
        self._api: Any = None
        self._ws: Any = None
        self._http: httpx.AsyncClient | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    async def start(self) -> None:
        from qqbot_agent_sdk import (
            EventParser,
            QQApiClient,
            QQWebSocket,
            WSCallbacks,
            WSSessionStore,
        )

        api = QQApiClient(app_id=self.app_id, client_secret=self.client_secret)
        # SDK 不自带 HTTP 客户端：不注入的话取网关地址和发消息都会报
        # "HTTP client not initialized"（README 的示例省略了这一步）
        self._http = httpx.AsyncClient(timeout=30)
        api.setup(self._http)
        self._api = api
        parser = EventParser()

        store: Any | None = None
        if self.session_store_dir:
            Path(self.session_store_dir).mkdir(parents=True, exist_ok=True)
            store = WSSessionStore(self.session_store_dir)

        # 当前连接的会话号、收到的最后一个事件的序号。平台每小时会断开一次连接，断线后拿这两样
        # 去续接，平台把断开期间的事件补发过来；它们同时存盘，重启后（一小时内）也能续接上，
        # 重启那几秒里的消息不丢。SDK 每收到一个事件就来问一次、存一次，所以以内存里的为准，
        # 存盘只是为了重启
        session: list[Any] = [None, None]
        if store is not None:
            kept = store.get(self.app_id)
            if kept.is_resumable and kept.is_fresh():
                session = [kept.session_id, kept.seq]

        def get_session() -> tuple[str | None, int | None]:
            return session[0], session[1]

        def set_session(session_id: str | None, seq: int | None) -> None:
            if [session_id, seq] == session:
                return
            session[0], session[1] = session_id, seq
            if store is None:
                return
            if session_id:
                store.save(self.app_id, session_id, seq=seq)
            else:
                store.clear(self.app_id)

        def on_connected() -> None:
            self.connected, self.connected_at = True, time.time()
            self.down_since, self.fatal = None, ""
            logger.info("qq_ws_connected")

        def on_disconnected() -> None:
            self.connected = False
            self.disconnects += 1
            self.down_since = self.down_since or time.time()
            logger.warning("qq_ws_disconnected")

        def on_fatal_error(code: str, message: str) -> None:
            # SDK 到这里就不再重连了。要不要重新连一条由 _watch 看原因决定
            self.connected, self.fatal = False, str(code)
            self.down_since = self.down_since or time.time()
            logger.error("qq_ws_fatal", code=code, message=message)

        async def on_message_event(event_type: str, raw: dict[str, Any]) -> None:
            self.events += 1
            self.last_event_at = time.time()
            addressed, text = True, None
            if event_type == GROUP_FULL_EVENT:
                # 这个群开了「全部消息」：字段和 @ 消息的事件一样，借 SDK 解析 @ 消息的那一套；
                # 是不是对机器人说的、文字里的 @ 标记，自己看
                addressed, text = mention_view(raw)
                event = parser.parse(GROUP_AT_EVENT, raw)
                mentions = raw.get("mentions")
                if not addressed and mentions and self._shape_logs > 0:
                    # 有人被 @ 了但没认出机器人：记下字段名（不记内容），
                    # 万一平台标记机器人用的不是 is_you，从这里能看出来
                    self._shape_logs -= 1
                    logger.info(
                        "qq_group_mention_not_me",
                        fields=sorted(raw),
                        mention_fields=[sorted(m) for m in mentions if isinstance(m, Mapping)],
                    )
            else:
                event = parser.parse(event_type, raw)
            if event is None:
                return
            author = raw.get("author")
            name = author.get("username") if isinstance(author, Mapping) else None
            inbound = InboundMessage(
                message_id=event.message_id,
                chat_scope=ChatScope(event.chat_scope),
                chat_id=event.chat_id,
                user_id=event.user_id,
                content=(event.content or "").strip() if text is None else text,
                timestamp=event.timestamp,
                user_name=event.user_name or (str(name) if name else None),
                group_id=event.chat_id if event.chat_scope == "group" else None,
                raw=event.raw or raw,
                images=image_refs(event.attachments),
            )
            # 没 @ 机器人的群消息，或者文字和图片都没有（空消息，只有语音、视频、文件）：不用回
            inbound.addressed = addressed and bool(inbound.content or inbound.images)
            if not inbound.addressed:
                try:
                    await self.on_message(inbound)  # 只给旁听的插件看一眼（记群聊记录），不回复
                except Exception:
                    logger.exception("observe_message_failed")
                return
            await self._answer(inbound)

        async def on_interaction_event(_event_type: str, raw: dict[str, Any]) -> None:
            """有人点了机器人消息上的按钮：当作他发了按钮带的那句指令。"""
            from qqbot_agent_sdk.dto import parse_interaction_event

            event = parse_interaction_event(raw)
            try:
                # 先告诉平台收到了：不然点的人那边按钮一直转圈，最后提示操作失败
                await api.acknowledge_interaction(event.id)
            except Exception as exc:
                logger.warning("qq_interaction_ack_failed", error=str(exc)[:300])
            command = event.data.resolved.button_data.strip()
            scope = ChatScope.GROUP if event.is_group else ChatScope.C2C if event.is_c2c else None
            if event.data.type != _INTERACTION_BUTTON or not command or scope is None:
                logger.info("qq_interaction_ignored", type=event.data.type, scene=event.scene)
                return
            inbound = InboundMessage(
                message_id=f"interaction:{event.id}",
                chat_scope=scope,
                chat_id=event.chat_id,
                user_id=event.operator_openid,
                content=command,
                timestamp=str(raw.get("timestamp") or ""),
                group_id=event.chat_id if scope == ChatScope.GROUP else None,
                raw={key: value for key, value in raw.items() if key != _ENVELOPE_ID},
                reply_event_id=str(raw.get(_ENVELOPE_ID) or "") or event.id,
            )
            await self._answer(inbound)

        self._loop = asyncio.get_running_loop()
        callbacks = WSCallbacks(
            on_message_event=guarded("qq_message_event_failed", on_message_event),
            on_interaction_event=guarded("qq_interaction_event_failed", on_interaction_event),
            on_connected=on_connected,
            on_disconnected=on_disconnected,
            on_fatal_error=on_fatal_error,
            get_token=api.ensure_token_sync,
            get_gateway_url=api.get_gateway_url_sync,
            get_session=get_session,
            set_session=set_session,
            set_heartbeat_interval=lambda interval: logger.debug(
                "qq_ws_heartbeat_interval", seconds=interval
            ),
            clear_token=api.clear_token,
            fail_pending=lambda reason: logger.warning("qq_ws_fail_pending", reason=reason),
        )

        async def connect() -> None:
            """连一条新的连接：启动时连一次；连接线程没了以后由 _watch 再调。"""
            ws = socket_class(QQWebSocket)(
                callbacks=callbacks, log_tag=f"JianShanAgent:{self.app_id}"
            )
            self._ws = ws
            await api.ensure_token()
            gateway_url = await api.get_gateway_url()
            ws.start(gateway_url, self._loop)

        self._connect = connect
        self._stopping = False
        self.down_since = time.time()
        await connect()
        self._watchdog = self._loop.create_task(self._watch())
        logger.info("qq_gateway_started", app_id=self.app_id)

    def health(self) -> tuple[bool, str]:
        """（和 QQ 的连接正不正常，不正常的话是怎么回事）。刚断开、正在重连算正常。"""
        if self.fatal and self.fatal not in _FATAL_RETRYABLE:
            return False, f"平台断开了连接，不会自动重连（{self.fatal}）"
        down = time.time() - self.down_since if self.down_since is not None else 0.0
        if down > self.unhealthy_after_seconds:
            return False, f"和 QQ 的连接已经断开 {down:.0f} 秒"
        return True, ""

    async def _watch(self) -> None:
        """看着连接：SDK 不再重连了，就重新连一条。

        两种情况 SDK 会一声不响地停下来，进程却照常活着、什么消息都收不到：
        启动时第一次握手就失败（连接线程直接退出，不重试也不通知）；重连了太多次、
        或者连上就断，它自己放弃了。平台不让连（下架、封禁、没有权限）时重连没有用，
        不去碰它，由健康检查报出来等人处理。
        """
        attempt = 0
        while not self._stopping:
            await asyncio.sleep(self.watch_seconds)
            if self._stopping:
                return
            ws = self._ws
            thread = getattr(ws, "_ws_thread", None)
            alive = thread is not None and thread.is_alive()
            if self.connected:
                attempt = 0
                continue
            if (alive and not self.fatal) or (self.fatal and self.fatal not in _FATAL_RETRYABLE):
                continue  # SDK 自己在重连；或者平台不让连
            delay = self.revive_delays[min(attempt, len(self.revive_delays) - 1)]
            attempt += 1
            logger.warning(
                "qq_ws_reviving", attempt=attempt, wait=delay, reason=self.fatal or "连接线程退出了"
            )
            await asyncio.sleep(delay)
            if self._stopping or self._connect is None:
                return
            try:
                if ws is not None:
                    await ws.async_stop()
                self.fatal = ""
                await self._connect()
            except Exception as exc:  # 这次没连上（取不到网关地址之类）：下一轮再来
                logger.warning("qq_ws_revive_failed", error=str(exc)[:300])

    async def _answer(self, inbound: InboundMessage) -> None:
        """把一条要回复的消息交给处理方，把它的进度和最终回复发出去。"""
        task = asyncio.current_task()
        if task is not None:
            self._inflight.add(task)  # 进程退出时会等它（见 _drain）
        try:
            await self._handle(inbound)
        finally:
            if task is not None:
                self._inflight.discard(task)

    async def _handle(self, inbound: InboundMessage) -> None:
        #: 用户发的那条消息（引用它）；这次是点按钮触发的话，没有这样一条消息
        asked = None if inbound.reply_event_id else inbound.message_id
        scope_name = inbound.chat_scope.value
        # 一条消息能回几次平台有明文；一次按钮点击能回几次文档没写，只发最终回复那一条
        replies_left = 1 if inbound.reply_event_id else self.max_replies.get(scope_name, 1)
        # 最后排进去的那句进度。每一句都等前一句发完才发，所以它发完了，前面的也都发完了
        queued: asyncio.Task[None] | None = None
        # 这条消息的进度发出去过没有：发出去的第一句引用用户的那条消息（让对方知道开始处理了），
        # 之后每一句引用机器人自己发的上一句（last_progress 是它的引用索引）
        started = False
        last_progress: str | None = None

        async def send_progress(
            outbound: OutboundMessage, previous: asyncio.Task[None] | None
        ) -> None:
            nonlocal started, last_progress
            if previous is not None:
                await previous
            try:
                if started:
                    sent = await self.send(inbound, outbound, quote=last_progress, own=True)
                else:
                    sent = await self.send(inbound, outbound, quote=asked)
            except Exception as exc:  # 进度发不出去不影响后面的进度和最终回复
                logger.warning("progress_send_failed", error=str(exc)[:300])
            else:
                started = True
                last_progress = sent or last_progress

        async def progress(outbound: OutboundMessage) -> None:
            nonlocal replies_left, queued
            if replies_left <= 1:  # 最后一次名额留给最终回复
                return
            replies_left -= 1  # 发没发成都算用掉一次：平台那边可能已经计数了
            # 放到后台去发，这里马上返回：等平台收下要一两秒，不能让处理方每句都停这么久
            queued = asyncio.create_task(send_progress(outbound, queued))

        async def progress_sent() -> None:
            """等排着的进度都发完：最终回复要排在它们后面。"""
            if queued is not None:
                await queued

        async def reply(outbound: OutboundMessage) -> None:
            """发最终回复；发出去了再通知旁听的一方（通知出错不算回复失败）。"""
            await self.send(inbound, outbound, quote=asked)
            if self.on_sent is not None:
                try:
                    await self.on_sent(inbound, outbound)
                except Exception:
                    logger.exception("observe_reply_failed")

        inbound.progress_sink = progress
        try:
            outbound = await self.on_message(inbound)
            await progress_sent()
            if outbound is None:
                return
            await reply(outbound)
        except asyncio.CancelledError:
            if self._stopping:
                # 进程在退出，等不到它处理完了：告诉对方一声，
                # 不然他看到「正在查资料…」之后就再没有下文（重启后平台也不会把这条消息再发一遍）
                try:
                    notice = OutboundMessage(content=_RESTART_NOTICE, reply_to=inbound.message_id)
                    await self.send(inbound, notice, quote=asked)
                except BaseException as exc:
                    logger.warning("restart_notice_failed", error=repr(exc)[:300])
            raise
        except Exception:
            logger.exception(
                "handle_message_failed",
                user_id=inbound.user_id,
                chat_id=inbound.chat_id,
            )
            try:
                await progress_sent()
                await reply(
                    OutboundMessage(
                        content="处理消息时出错了，请稍后再试。",
                        reply_to=inbound.message_id,
                    )
                )
            except Exception:
                logger.exception("error_reply_failed")
        finally:
            # 正常走完时进度早就发完了。还有没发完的，只能是处理被取消了（进程在退出）：
            # 一并取消，不留下没人管的任务
            if queued is not None and not queued.done():
                queued.cancel()

    async def send(
        self,
        inbound: InboundMessage,
        outbound: OutboundMessage,
        *,
        quote: str | None = None,
        own: bool = False,
    ) -> str | None:
        """发一条消息；返回平台给它的引用索引（以后要引用这条消息时用，没拿到就是 None）。

        ``quote`` 是要引用的那条消息（引用索引或消息 id），None = 不引用；
        ``own`` 表示引用的是机器人自己发的消息。

        从最想要的发法往最保守的试：带引用的 Markdown → 不带引用的 Markdown → 带引用的纯文本
        → 不带引用的纯文本（本来就是纯文本的消息只有后两种）。引用、Markdown、按钮都只是为了好看，
        不能因为平台不收它们让用户收不到回复。某一种引用连着被拒 ``_QUOTE_GIVE_UP`` 次以后
        就不再试它，免得每条消息都白发一次。

        连最保守的发法平台都明说不收、而文字里带着链接时，去掉链接再发一次：平台只让发报备过的
        域名的链接，带了别的整条消息都会被拒——回答的文字比链接要紧。
        """
        if self._api is None:
            raise RuntimeError("gateway 未启动")
        reply_to = outbound.reply_to or inbound.message_id
        rich = outbound.markdown or bool(outbound.buttons)
        plain = markdown_to_text(outbound.content) if rich else outbound.content
        if inbound.chat_scope not in (ChatScope.C2C, ChatScope.GROUP):
            # 频道：用 SDK 自带的发送（纯文本，引用被回复的那条）
            await self._api.send_text(
                inbound.chat_scope.value, inbound.chat_id, plain, reply_to=reply_to, markdown=False
            )
            return None

        target = "groups" if inbound.chat_scope == ChatScope.GROUP else "users"
        path = f"/v2/{target}/{inbound.chat_id}/messages"
        rejections = self._quote_rejections
        forms = [
            (markdown, quoted)
            for markdown in ((True, False) if rich else (False,))
            for quoted in (True, False)
            if not quoted or (quote and rejections.get((own, markdown), 0) < _QUOTE_GIVE_UP)
        ]
        #: 刚刚带着引用没发出去的那种格式（是不是 Markdown）
        refused: bool | None = None

        def make_body(text: str, *, markdown: bool, quoted: bool) -> dict[str, Any]:
            body: dict[str, Any] = self._api.build_text_body(
                text, reply_to=reply_to, markdown=markdown
            ).to_dict()
            # SDK 给纯文本消息一律加上「引用被回复的那条」；引用谁由这里定
            body.pop("message_reference", None)
            if quoted:
                body["message_reference"] = {"message_id": quote}
            if inbound.reply_event_id:
                # 回的是一次按钮点击：挂在点击事件上，不是挂在哪条消息上
                body.pop("msg_id", None)
                body["event_id"] = inbound.reply_event_id
            if markdown and outbound.buttons:
                body["keyboard"] = build_keyboard(outbound.buttons, user_id=inbound.user_id)
            return body

        for position, (markdown, quoted) in enumerate(forms):
            body = make_body(
                outbound.content if markdown else plain, markdown=markdown, quoted=quoted
            )
            last = position == len(forms) - 1
            try:
                response = await self._post(path, body, retry=last)
            except Exception as exc:
                links = urls_in(plain) if last and refused_by_platform(exc) else set()
                if links:
                    logger.warning(
                        "qq_send_refused_trying_without_links",
                        chat_id=inbound.chat_id,
                        links=len(links),
                        error=str(exc)[:300],
                    )
                    bare = strip_unknown_links(plain, set()).rstrip() + "\n" + _LINKS_DROPPED
                    response = await self._post(
                        path, make_body(bare, markdown=False, quoted=False), retry=True
                    )
                    return sent_ref(response)
                if last:
                    raise
                logger.warning(
                    "qq_send_failed_trying_plainer",
                    markdown=markdown,
                    quoted=quoted,
                    chat_id=inbound.chat_id,
                    error=str(exc)[:300],
                )
                # 只有平台明说不收，才算在「这种引用发不出去」头上；超时、平台一时出故障不算
                refused = markdown if quoted and refused_by_platform(exc) else None
                continue
            key = (own, markdown)
            if quoted:
                rejections.pop(key, None)
            elif refused == markdown:
                # 同一种格式，带着引用发不出去、去掉就发出去了：平台不收的是这种引用
                rejections[key] = rejections.get(key, 0) + 1
                if rejections[key] == _QUOTE_GIVE_UP:
                    logger.warning("qq_quote_given_up", own=own, markdown=markdown)
            return sent_ref(response)
        raise AssertionError("至少有一种发法")  # forms 里一定有不带引用的纯文本

    async def _post(self, path: str, body: dict[str, Any], *, retry: bool) -> dict[str, Any]:
        """把一条消息发给平台。

        ``retry``：遇到一时的故障（超时、5xx）等一等再试，最多 ``_SEND_ATTEMPTS`` 次；
        请求本身不对的（平台明说了不收）不重试。
        """
        from qqbot_agent_sdk.utils import is_fatal_send_error

        attempts = _SEND_ATTEMPTS if retry else 1
        for attempt in range(attempts):
            try:
                response = await self._api.request("POST", path, body)
            except Exception as exc:
                if attempt == attempts - 1 or is_fatal_send_error(str(exc)):
                    raise
                await asyncio.sleep(self.retry_base_seconds * 2**attempt)
            else:
                return response if isinstance(response, dict) else {}
        raise AssertionError("循环里要么返回要么抛出")

    async def _drain(self) -> None:
        """进程退出前：等正在处理的消息处理完；到点还没完的，回一句「在重启」再走。"""
        pending = set(self._inflight)
        if not pending:
            return
        logger.info("qq_gateway_draining", messages=len(pending), seconds=self.drain_seconds)
        _done, pending = await asyncio.wait(pending, timeout=self.drain_seconds)
        if not pending:
            return
        logger.warning("qq_gateway_drain_cut", messages=len(pending))
        for task in pending:
            task.cancel()  # _handle 接到取消会回那一句
        await asyncio.wait(pending, timeout=_NOTICE_SECONDS)

    async def stop(self) -> None:
        self._stopping = True
        if self._watchdog is not None:
            self._watchdog.cancel()
            await asyncio.wait([self._watchdog])  # 等它真的停下，不留下没人管的任务
            self._watchdog = None
        if self._ws is not None:
            # 连接跑在 SDK 的独立线程里，要用 async_stop 等它退出。先停它：不再收新的消息
            await self._ws.async_stop()
            self._ws = None
        await self._drain()  # 发消息用的 HTTP 客户端要等它们发完回复再关
        if self._http is not None:
            await self._http.aclose()
            self._http = None
        logger.info("qq_gateway_stopped")
