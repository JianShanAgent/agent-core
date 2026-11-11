"""QQ 网关适配层：用 SDK 真实的事件解析器，只把联网的两个类换成记录调用的替身。"""

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import qqbot_agent_sdk
from qqbot_agent_sdk import QQApiClient as RealApiClient

from jianshanagent.core.messages import Button, ChatScope, ImageRef, InboundMessage, OutboundMessage
from jianshanagent.gateway.qq import QQGateway, sent_ref

# 官方文档 GROUP_AT_MESSAGE_CREATE 事件示例的形状
GROUP_EVENT = {
    "id": "ROBOT1.0_msg",
    "author": {"id": "MEMBER", "member_openid": "MEMBER", "member_role": "member"},
    "content": " /查 山风 ",
    "group_openid": "GROUP",
    "message_type": 0,
    "timestamp": "2026-07-21T10:00:00+08:00",
}


class FakeApi:
    instances: list["FakeApi"] = []

    def __init__(self, app_id: str, client_secret: str):
        self.http: Any = None
        #: 发出去的消息：``requests`` 是原始请求，``sent`` 是整理过的样子，一一对应
        self.requests: list[dict[str, Any]] = []
        self.sent: list[dict[str, Any]] = []
        #: 平台明说了不收的请求体（没发出去）
        self.refused: list[dict[str, Any]] = []
        self.reject_markdown = False
        #: 请求体让它返回 True 的，平台不收
        self.refuse: Callable[[dict[str, Any]], bool] = lambda body: False
        #: 发这些内容的消息时平台出故障（500）
        self.reject_texts: set[str] = set()
        #: 接下来这么多个请求，平台一时出故障（500），之后恢复
        self.outages = 0
        #: 发这些内容的消息时，要等到对应的信号放行才算发出去（平台处理得慢）
        self.hold: dict[str, asyncio.Event] = {}
        #: 响应里带不带引用索引（文档里两种响应都有）
        self.give_ref_idx = True
        #: 每条发出去的消息拿到的引用索引，按内容查（同样内容发过几次的，是最近那次的）
        self.ref_of: dict[str, str] = {}
        self.count = 0
        #: 告诉过平台「收到了」的按钮点击
        self.acknowledged: list[str] = []
        self.reject_ack = False
        FakeApi.instances.append(self)

    def setup(self, http_client: Any) -> None:
        self.http = http_client

    def _require_http(self) -> None:
        # 与真实 SDK 一致：没注入 HTTP 客户端就报错
        if self.http is None:
            raise RuntimeError("HTTP client not initialized — not connected?")

    async def ensure_token(self) -> str:
        return "token"

    async def get_gateway_url(self) -> str:
        self._require_http()
        return "wss://gateway.example"

    def ensure_token_sync(self) -> str:
        return "token"

    def get_gateway_url_sync(self) -> str:
        return "wss://gateway.example"

    def clear_token(self) -> None:
        pass

    build_text_body = staticmethod(RealApiClient.build_text_body)

    async def acknowledge_interaction(self, interaction_id: str) -> None:
        self._require_http()
        if self.reject_ack:
            raise RuntimeError("QQ Bot API error [500]: 服务内部错误")
        self.acknowledged.append(interaction_id)

    async def request(self, method: str, path: str, body: dict[str, Any]) -> dict[str, Any]:
        self._require_http()
        if self.http.is_closed:  # 与真实的 HTTP 客户端一致：关了以后什么都发不出去
            raise RuntimeError("Cannot send a request, as the client has been closed.")
        markdown = "markdown" in body
        content = body["markdown"]["content"] if markdown else body["content"]
        if content in self.hold:
            await self.hold[content].wait()
        if markdown and self.reject_markdown:
            self.refused.append(body)
            raise RuntimeError("QQ Bot API error [400]: 无markdown模板权限")
        if self.refuse(body):
            self.refused.append(body)
            raise RuntimeError("QQ Bot API error [400]: invalid request")
        if content in self.reject_texts:
            raise RuntimeError("QQ Bot API error [500]: 服务内部错误")
        if self.outages > 0:
            self.outages -= 1
            raise RuntimeError("QQ Bot API error [500]: 服务内部错误")
        _, _, kind, chat_id, _ = path.split("/")  # /v2/groups/<群>/messages
        self.requests.append({"method": method, "path": path, "body": body})
        self.sent.append(
            {
                "chat_type": "group" if kind == "groups" else "c2c",
                "chat_id": chat_id,
                "content": content,
                "reply_to": body.get("msg_id"),
                "markdown": markdown,
                "quote": (body.get("message_reference") or {}).get("message_id"),
            }
        )
        self.count += 1
        response: dict[str, Any] = {"id": f"ROBOT1.0_bot{self.count}"}
        if self.give_ref_idx:
            response["ext_info"] = {"ref_idx": f"REFIDX_bot{self.count}"}
        self.ref_of[content] = sent_ref(response) or ""
        return response


class FakeThread:
    """SDK 的连接线程。"""

    def __init__(self) -> None:
        self.alive = True

    def is_alive(self) -> bool:
        return self.alive


class FakeWebSocket:
    instances: list["FakeWebSocket"] = []

    def __init__(self, callbacks: Any, log_tag: str = ""):
        self.callbacks = callbacks
        self.started_with: str | None = None
        self.stopped = False
        self._ws_thread: FakeThread | None = None
        FakeWebSocket.instances.append(self)

    def start(self, gateway_url: str, main_loop: Any) -> None:
        self.started_with = gateway_url
        self._ws_thread = FakeThread()

    async def async_stop(self) -> None:
        self.stopped = True

    async def stop(self) -> None:
        raise AssertionError("连接跑在独立线程里，应调用 async_stop()")


@pytest.fixture
def sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeApi.instances.clear()
    FakeWebSocket.instances.clear()
    monkeypatch.setattr(qqbot_agent_sdk, "QQApiClient", FakeApi)
    monkeypatch.setattr(qqbot_agent_sdk, "QQWebSocket", FakeWebSocket)


def _gateway(
    handler: Any,
    tmp_path: Path,
    *,
    max_replies: dict[str, int] | None = None,
    on_sent: Any = None,
    **options: Any,
) -> QQGateway:
    return QQGateway(
        app_id="1234",
        client_secret="secret",
        on_message=handler,
        on_sent=on_sent,
        session_store_dir=str(tmp_path / "ws"),
        max_replies=max_replies,
        retry_base_seconds=0,
        **options,
    )


async def test_start_injects_http_client_and_stop_shuts_everything_down(
    sdk: None, tmp_path: Path
) -> None:
    async def handler(_message: InboundMessage) -> OutboundMessage | None:
        return None

    gateway = _gateway(handler, tmp_path)
    await gateway.start()  # 没注入 HTTP 客户端的话，这里取网关地址就会报错
    api, ws = FakeApi.instances[0], FakeWebSocket.instances[0]

    assert ws.started_with == "wss://gateway.example"
    assert api.http is not None and not api.http.is_closed

    await gateway.stop()
    assert ws.stopped is True
    assert api.http.is_closed


async def test_group_event_is_routed_and_reply_is_sent_as_passive_text(
    sdk: None, tmp_path: Path
) -> None:
    received: list[InboundMessage] = []

    async def handler(message: InboundMessage) -> OutboundMessage | None:
        received.append(message)
        return OutboundMessage(content="【山风】", reply_to=message.message_id)

    gateway = _gateway(handler, tmp_path)
    await gateway.start()
    api, ws = FakeApi.instances[0], FakeWebSocket.instances[0]
    await ws.callbacks.on_message_event("GROUP_AT_MESSAGE_CREATE", GROUP_EVENT)
    await gateway.stop()

    message = received[0]
    assert (message.chat_scope, message.chat_id, message.user_id) == (
        ChatScope.GROUP,
        "GROUP",
        "MEMBER",
    )
    assert message.content == "/查 山风"
    assert message.session_key == "group:GROUP:user:MEMBER"
    assert api.sent == [
        {
            "chat_type": "group",
            "chat_id": "GROUP",
            "content": "【山风】",
            "reply_to": "ROBOT1.0_msg",
            "markdown": False,
            "quote": "ROBOT1.0_msg",  # 最终回复引用用户的那条消息
        }
    ]


async def test_handler_failure_sends_apology_and_silence_sends_nothing(
    sdk: None, tmp_path: Path
) -> None:
    async def handler(message: InboundMessage) -> OutboundMessage | None:
        if "炸" in message.content:
            raise RuntimeError("boom")
        return None

    gateway = _gateway(handler, tmp_path)
    await gateway.start()
    api, ws = FakeApi.instances[0], FakeWebSocket.instances[0]
    await ws.callbacks.on_message_event("GROUP_AT_MESSAGE_CREATE", GROUP_EVENT)
    await ws.callbacks.on_message_event(
        "GROUP_AT_MESSAGE_CREATE", {**GROUP_EVENT, "id": "m2", "content": "炸"}
    )
    await ws.callbacks.on_message_event("UNKNOWN_EVENT", GROUP_EVENT)
    await gateway.stop()

    assert [(s["reply_to"], s["quote"], s["content"]) for s in api.sent] == [
        ("m2", "m2", "处理消息时出错了，请稍后再试。")
    ]


async def test_markdown_with_buttons_and_fallback_to_text(sdk: None, tmp_path: Path) -> None:
    async def handler(message: InboundMessage) -> OutboundMessage | None:
        return OutboundMessage(
            content="**【山风】** · SSR\n> 森林之王\n[查看 wiki 页面](https://w.example/x)",
            reply_to=message.message_id,
            markdown=True,
            buttons=[Button("山风", "/查 山风"), Button("大天狗·高天铁羽的超长名字", "/查 x")],
        )

    gateway = _gateway(handler, tmp_path)
    await gateway.start()
    api, ws = FakeApi.instances[0], FakeWebSocket.instances[0]
    await ws.callbacks.on_message_event("GROUP_AT_MESSAGE_CREATE", GROUP_EVENT)

    request = api.requests[0]
    assert (request["method"], request["path"]) == ("POST", "/v2/groups/GROUP/messages")
    body = request["body"]
    assert (body["msg_type"], body["msg_id"]) == (2, "ROBOT1.0_msg")
    assert body["markdown"]["content"].startswith("**【山风】**")
    assert body["message_reference"] == {"message_id": "ROBOT1.0_msg"}  # Markdown 回复也引用
    buttons = body["keyboard"]["content"]["rows"][0]["buttons"]
    assert [b["render_data"]["label"] for b in buttons] == ["山风", "大天狗·高天铁羽的…"]
    # 白底红字；点了由平台直接通知机器人（回调），不是把字填进输入框；只有提问的人能点
    assert [b["render_data"]["style"] for b in buttons] == [3, 3]
    assert buttons[0]["action"] == {
        "type": 1,
        "permission": {"type": 0, "specify_user_ids": ["MEMBER"]},
        "data": "/查 山风",
        "unsupport_tips": "当前版本不支持按钮，请直接发送指令",
    }

    # 平台拒绝 Markdown 时，用户仍然要收到一条纯文本（照样引用）
    api.reject_markdown = True
    await ws.callbacks.on_message_event("GROUP_AT_MESSAGE_CREATE", {**GROUP_EVENT, "id": "m2"})
    await gateway.stop()

    assert api.sent[1:] == [
        {
            "chat_type": "group",
            "chat_id": "GROUP",
            "content": "【山风】 · SSR\n森林之王\n查看 wiki 页面 https://w.example/x",
            "reply_to": "m2",
            "markdown": False,
            "quote": "m2",
        }
    ]
    assert len(api.refused) == 2  # 带引用、不带引用的 Markdown 各试过一次


# 官方文档 INTERACTION_CREATE 事件的形状（群里有人点了机器人消息上的按钮）；
# ``_envelope_id`` 是事件最外层的 id，由 socket_class 放进来
CLICK_EVENT = {
    "id": "CLICK1",
    "type": 11,
    "scene": "group",
    "chat_type": 1,
    "timestamp": "2026-07-21T10:00:05+08:00",
    "group_openid": "GROUP",
    "group_member_openid": "MEMBER",
    "version": 1,
    "data": {"type": 11, "resolved": {"button_data": "/查 山风", "button_id": "0"}},
    "_envelope_id": "INTERACTION_CREATE:envelope1",
}


async def test_a_button_click_is_handled_as_the_command_and_answered_on_the_click_event(
    sdk: None, tmp_path: Path
) -> None:
    received: list[InboundMessage] = []
    observed: list[tuple[str, str]] = []

    async def handler(message: InboundMessage) -> OutboundMessage | None:
        received.append(message)
        await message.progress("正在查…")  # 点击能回几次文档没写：进度不发
        return OutboundMessage(
            content="**【山风】**",
            reply_to=message.message_id,
            markdown=True,
            buttons=[Button("山风·御魂", "/查 山风 御魂")],
        )

    async def on_sent(inbound: InboundMessage, outbound: OutboundMessage) -> None:
        observed.append((inbound.message_id, outbound.content))

    gateway = _gateway(handler, tmp_path, max_replies={"group": 5}, on_sent=on_sent)
    await gateway.start()
    api, ws = FakeApi.instances[0], FakeWebSocket.instances[0]
    await ws.callbacks.on_interaction_event("INTERACTION_CREATE", CLICK_EVENT)

    assert api.acknowledged == ["CLICK1"]  # 先告诉平台收到了，点的人那边按钮才不会一直转圈
    message = received[0]
    assert (message.chat_scope, message.chat_id, message.user_id, message.content) == (
        ChatScope.GROUP,
        "GROUP",
        "MEMBER",
        "/查 山风",
    )
    assert message.addressed is True
    assert message.session_key == "group:GROUP:user:MEMBER"  # 和他自己发这句指令是同一段对话
    assert "_envelope_id" not in message.raw  # 自己加的键不留在事件原文里

    assert len(api.requests) == 1
    body = api.requests[0]["body"]
    assert api.requests[0]["path"] == "/v2/groups/GROUP/messages"
    # 回复挂在点击事件（最外层的 id）上；没有哪条用户消息可挂、可引用
    assert body["event_id"] == "INTERACTION_CREATE:envelope1"
    assert "msg_id" not in body and "message_reference" not in body
    # 回复里的新按钮，还是只有点的这个人能点
    permission = body["keyboard"]["content"]["rows"][0]["buttons"][0]["action"]["permission"]
    assert permission == {"type": 0, "specify_user_ids": ["MEMBER"]}
    assert observed == [("interaction:CLICK1", "**【山风】**")]  # 照常通知旁听的一方

    # 单聊里的点击；告诉平台「收到了」失败也照样处理
    api.reject_ack = True
    c2c_click = {
        key: value
        for key, value in CLICK_EVENT.items()
        if key not in ("group_openid", "group_member_openid", "_envelope_id")
    }
    await ws.callbacks.on_interaction_event(
        "INTERACTION_CREATE",
        {**c2c_click, "id": "CLICK2", "scene": "c2c", "chat_type": 2, "user_openid": "USER"},
    )
    assert (received[1].chat_scope, received[1].chat_id, received[1].user_id) == (
        ChatScope.C2C,
        "USER",
        "USER",
    )
    assert api.requests[1]["path"] == "/v2/users/USER/messages"
    assert api.requests[1]["body"]["event_id"] == "CLICK2"  # 没拿到最外层的 id 时用事件自己的

    # 不是点消息按钮的事件（机器人配置页面的查询）、按钮上没带指令的：收下，不处理
    for odd in (
        {**CLICK_EVENT, "id": "CLICK3", "data": {"type": 2001, "resolved": {}}},
        {**CLICK_EVENT, "id": "CLICK4", "data": {"type": 11, "resolved": {"button_data": " "}}},
    ):
        await ws.callbacks.on_interaction_event("INTERACTION_CREATE", odd)
    await gateway.stop()
    assert len(received) == 2 and len(api.requests) == 2


# 官方文档 C2C_MESSAGE_CREATE 事件示例的形状
C2C_EVENT = {
    "id": "ROBOT1.0_c2c",
    "author": {"user_openid": "USER"},
    "content": "水豚活动什么时候开",
    "timestamp": "2026-07-21T10:00:00+08:00",
}


async def _six_steps(message: InboundMessage) -> OutboundMessage | None:
    """处理过程中报 6 次进度，最后回一条结论。"""
    for number in range(1, 7):
        await message.progress(f"进度 {number}")
    return OutboundMessage(content="结论", reply_to=message.message_id)


async def test_progress_messages_leave_the_last_reply_slot_for_the_answer(
    sdk: None, tmp_path: Path
) -> None:
    gateway = _gateway(_six_steps, tmp_path, max_replies={"c2c": 4, "group": 5})
    await gateway.start()
    api, ws = FakeApi.instances[0], FakeWebSocket.instances[0]

    # 单聊一条消息最多回复 4 次：3 句进度 + 结论，多出来的进度不发。进度是单独的纯文本被动回复
    await ws.callbacks.on_message_event("C2C_MESSAGE_CREATE", C2C_EVENT)
    assert [(s["chat_type"], s["chat_id"], s["reply_to"], s["markdown"]) for s in api.sent] == [
        ("c2c", "USER", "ROBOT1.0_c2c", False)
    ] * 4
    assert [s["content"] for s in api.sent] == ["进度 1", "进度 2", "进度 3", "结论"]
    # 第一句进度和结论引用用户的那条消息；中间每一句引用机器人自己的上一句
    assert [s["quote"] for s in api.sent] == [
        "ROBOT1.0_c2c",
        api.ref_of["进度 1"],
        api.ref_of["进度 2"],
        "ROBOT1.0_c2c",
    ]
    assert api.ref_of["进度 1"].startswith("REFIDX_")  # 引用自己的消息用平台给的引用索引

    # 群聊最多 5 次：4 句进度 + 结论，做法和单聊一样
    api.sent.clear()
    await ws.callbacks.on_message_event("GROUP_AT_MESSAGE_CREATE", GROUP_EVENT)
    assert [s["content"] for s in api.sent] == ["进度 1", "进度 2", "进度 3", "进度 4", "结论"]
    assert [s["quote"] for s in api.sent][::4] == ["ROBOT1.0_msg", "ROBOT1.0_msg"]
    assert api.sent[3]["quote"] == api.ref_of["进度 3"]

    # 进度发不出去：照样算用掉一次名额，不影响后面的进度和结论；下一句引用的是发出去了的那一句
    api.sent.clear()
    api.reject_texts = {"进度 2"}
    await ws.callbacks.on_message_event("C2C_MESSAGE_CREATE", {**C2C_EVENT, "id": "m2"})
    assert [s["content"] for s in api.sent] == ["进度 1", "进度 3", "结论"]
    assert [s["quote"] for s in api.sent] == ["m2", api.ref_of["进度 1"], "m2"]

    # 第一句就没发出去：用户实际看到的第一句（进度 2）引用他的消息
    api.sent.clear()
    api.reject_texts = {"进度 1"}
    await ws.callbacks.on_message_event("C2C_MESSAGE_CREATE", {**C2C_EVENT, "id": "m4"})
    assert [s["quote"] for s in api.sent] == ["m4", api.ref_of["进度 2"], "m4"]

    # 平台的响应里没有引用索引：退回用消息 id 引用
    api.sent.clear()
    api.reject_texts, api.give_ref_idx = set(), False
    await ws.callbacks.on_message_event("C2C_MESSAGE_CREATE", {**C2C_EVENT, "id": "m3"})
    await gateway.stop()
    assert api.sent[1]["quote"] == api.ref_of["进度 1"] and api.sent[1]["quote"].startswith("ROBOT")


async def test_quote_the_platform_refuses_is_dropped_and_soon_no_longer_tried(
    sdk: None, tmp_path: Path
) -> None:
    async def handler(message: InboundMessage) -> OutboundMessage | None:
        await message.progress("进度 1")
        await message.progress("进度 2")
        return OutboundMessage(content="**结论**", reply_to=message.message_id, markdown=True)

    gateway = _gateway(handler, tmp_path, max_replies={"c2c": 4})
    await gateway.start()
    api, ws = FakeApi.instances[0], FakeWebSocket.instances[0]
    api.refuse = lambda body: "markdown" in body and "message_reference" in body
    for number in range(3):
        event = {**C2C_EVENT, "id": f"m{number}"}
        await ws.callbacks.on_message_event("C2C_MESSAGE_CREATE", event)
    await gateway.stop()

    # 平台不收带引用的 Markdown：三条结论都照样以 Markdown 发出去了，只是没有引用
    finals = [(s["content"], s["quote"]) for s in api.sent if s["markdown"]]
    assert finals == [("**结论**", None)] * 3
    assert len(api.refused) == 2  # 连着被拒两次以后不再试，第三条没有白发
    # 别的引用不受影响：进度照样引用上一句
    assert all(s["quote"] for s in api.sent if s["content"] == "进度 2")


async def test_a_reply_the_platform_refuses_for_its_links_is_sent_again_without_them(
    sdk: None, tmp_path: Path
) -> None:
    async def handler(message: InboundMessage) -> OutboundMessage | None:
        if message.content == "没有链接":
            return OutboundMessage(content="平台不收的内容", reply_to=message.message_id)
        return OutboundMessage(
            content="**3 日开**，见 [活动公告](https://unlisted.example/a?b=1)（官方）",
            reply_to=message.message_id,
            markdown=True,
        )

    gateway = _gateway(handler, tmp_path)
    await gateway.start()
    api, ws = FakeApi.instances[0], FakeWebSocket.instances[0]
    # 平台只让发报备过的域名的链接，带了别的整条消息被拒——四种发法都一样
    api.refuse = lambda body: "unlisted.example" in str(body) or "不收的" in str(body)
    await ws.callbacks.on_message_event("C2C_MESSAGE_CREATE", C2C_EVENT)
    # 去掉链接再发一次：回答的文字比链接要紧，不能让对方只收到一句「处理消息时出错了」
    assert [(s["content"], s["reply_to"], s["markdown"]) for s in api.sent] == [
        ("3 日开，见 活动公告 （官方）\n（链接发不出去，已省略）", "ROBOT1.0_c2c", False)
    ]

    # 没有链接可去的，照旧：发不出去就道歉
    api.sent.clear()
    event = {**C2C_EVENT, "id": "m2", "content": "没有链接"}
    await ws.callbacks.on_message_event("C2C_MESSAGE_CREATE", event)
    await gateway.stop()
    assert [s["content"] for s in api.sent] == ["处理消息时出错了，请稍后再试。"]


async def test_a_passing_outage_does_not_count_as_the_platform_refusing_quotes(
    sdk: None, tmp_path: Path
) -> None:
    async def handler(message: InboundMessage) -> OutboundMessage | None:
        return OutboundMessage(content="结论", reply_to=message.message_id)

    gateway = _gateway(handler, tmp_path)
    await gateway.start()
    api, ws = FakeApi.instances[0], FakeWebSocket.instances[0]
    # 前三条消息带引用的那一次都碰上平台一时出故障（500），去掉引用马上就发出去了
    for number in range(3):
        api.outages = 1
        await ws.callbacks.on_message_event("C2C_MESSAGE_CREATE", {**C2C_EVENT, "id": f"m{number}"})
    # 平台恢复以后照样带引用：故障不是「平台不收这种引用」，不能因此以后都不引用了
    await ws.callbacks.on_message_event("C2C_MESSAGE_CREATE", {**C2C_EVENT, "id": "m3"})
    await gateway.stop()
    assert [(s["reply_to"], s["quote"]) for s in api.sent] == [
        ("m0", None),
        ("m1", None),
        ("m2", None),
        ("m3", "m3"),
    ]


async def test_an_event_that_cannot_be_parsed_leaves_a_trace_and_does_not_stop_the_rest(
    sdk: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    received: list[str] = []
    logged: list[tuple[str, dict[str, Any]]] = []

    async def handler(message: InboundMessage) -> OutboundMessage | None:
        received.append(message.content)
        return None

    def record(event: str, **fields: Any) -> None:
        logged.append((event, fields))

    monkeypatch.setattr("jianshanagent.gateway.qq.logger.exception", record)
    gateway = _gateway(handler, tmp_path)
    await gateway.start()
    ws = FakeWebSocket.instances[0]
    # 平台发来形状不对的事件（解析时就抛异常）：SDK 不看回调的结果，这里不记就一点痕迹都没有
    await ws.callbacks.on_interaction_event("INTERACTION_CREATE", {"id": "c", "data": {"type": {}}})
    await ws.callbacks.on_message_event("C2C_MESSAGE_CREATE", {**C2C_EVENT, "attachments": 7})
    await ws.callbacks.on_message_event("C2C_MESSAGE_CREATE", C2C_EVENT)
    await gateway.stop()
    assert [(event, fields["type"]) for event, fields in logged] == [
        ("qq_interaction_event_failed", "INTERACTION_CREATE"),
        ("qq_message_event_failed", "C2C_MESSAGE_CREATE"),
    ]
    assert received == ["水豚活动什么时候开"]  # 后面正常的事件照常处理


def test_sent_ref_is_none_when_the_platform_gives_nothing_to_quote_by() -> None:
    assert sent_ref({"id": "ROBOT1.0_x", "ext_info": {"ref_idx": "REFIDX_x"}}) == "REFIDX_x"
    assert sent_ref({"ext_info": None}) is None


async def test_final_replies_that_went_out_are_reported_but_progress_is_not(
    sdk: None, tmp_path: Path
) -> None:
    reported: list[tuple[str, str]] = []

    async def handler(message: InboundMessage) -> OutboundMessage | None:
        if "炸" in message.content:
            raise RuntimeError("boom")
        if "不回" in message.content:
            return None
        await message.progress("正在查…")
        return OutboundMessage(content="结论", reply_to=message.message_id)

    async def on_sent(inbound: InboundMessage, outbound: OutboundMessage) -> None:
        reported.append((inbound.message_id, outbound.content))
        raise RuntimeError("记录的一方出错了")  # 不能连累回复

    gateway = _gateway(handler, tmp_path, max_replies={"group": 5}, on_sent=on_sent)
    await gateway.start()
    api, ws = FakeApi.instances[0], FakeWebSocket.instances[0]
    for number, content in enumerate(["超鬼王是什么", "不回", "炸"], start=1):
        event = {**GROUP_EVENT, "id": f"m{number}", "content": content}
        await ws.callbacks.on_message_event("GROUP_AT_MESSAGE_CREATE", event)
    await gateway.stop()

    assert [s["content"] for s in api.sent] == ["正在查…", "结论", "处理消息时出错了，请稍后再试。"]
    # 只报最终回复（出错时的那句道歉也是最终回复），进度不报；没回复的消息不报
    assert reported == [("m1", "结论"), ("m3", "处理消息时出错了，请稍后再试。")]

    # 回复没发出去：不报
    reported.clear()
    gateway = _gateway(handler, tmp_path, on_sent=on_sent)
    await gateway.start()
    api, ws = FakeApi.instances[-1], FakeWebSocket.instances[-1]
    api.reject_texts = {"结论", "处理消息时出错了，请稍后再试。"}
    await ws.callbacks.on_message_event("GROUP_AT_MESSAGE_CREATE", {**GROUP_EVENT, "id": "m9"})
    await gateway.stop()
    assert reported == [] and api.sent == []


async def test_no_progress_messages_when_the_reply_limit_is_unknown(
    sdk: None, tmp_path: Path
) -> None:
    gateway = _gateway(_six_steps, tmp_path)
    await gateway.start()
    api, ws = FakeApi.instances[0], FakeWebSocket.instances[0]
    await ws.callbacks.on_message_event("C2C_MESSAGE_CREATE", C2C_EVENT)
    await gateway.stop()
    assert [s["content"] for s in api.sent] == ["结论"]


async def test_progress_is_sent_in_the_background_in_order_and_before_the_final_reply(
    sdk: None, tmp_path: Path
) -> None:
    release = asyncio.Event()
    sent_meanwhile: list[str] = []

    async def handler(message: InboundMessage) -> OutboundMessage | None:
        await message.progress("进度 1")  # 平台迟迟不收下这一句
        await message.progress("进度 2")
        await asyncio.sleep(0.01)  # 给后台留出发送的机会
        sent_meanwhile.extend(s["content"] for s in api.sent)
        release.set()
        if message.content == "炸":
            raise RuntimeError("boom")
        if message.content == "不用回":
            return None
        return OutboundMessage(content="结论", reply_to=message.message_id)

    gateway = _gateway(handler, tmp_path, max_replies={"c2c": 4})
    await gateway.start()
    api, ws = FakeApi.instances[0], FakeWebSocket.instances[0]
    api.hold = {"进度 1": release}
    # 正常回复、处理出错、不回复：最后那条（结论 / 道歉）都排在两句进度后面
    endings = {"问": ["结论"], "炸": ["处理消息时出错了，请稍后再试。"], "不用回": []}
    for number, (content, ending) in enumerate(endings.items()):
        api.sent.clear()
        release.clear()
        event = {**C2C_EVENT, "id": f"m{number}", "content": content}
        # 进度卡在平台那边时，处理照样往下走（会卡住的话这里超时）
        await asyncio.wait_for(ws.callbacks.on_message_event("C2C_MESSAGE_CREATE", event), 1)
        assert [s["content"] for s in api.sent] == ["进度 1", "进度 2", *ending]
        # 处理完没有留下还在跑的后台任务（看着连接的那一个是网关自己的，一直在）
        assert asyncio.all_tasks() - {gateway._watchdog} == {asyncio.current_task()}
    await gateway.stop()
    # 处理过程中一句都还没发出去：第一句卡着，第二句也不会抢到它前面
    assert sent_meanwhile == []


async def test_cancelled_handling_cancels_the_progress_still_queued(
    sdk: None, tmp_path: Path
) -> None:
    never = asyncio.Event()

    async def handler(message: InboundMessage) -> OutboundMessage | None:
        await message.progress("进度 1")
        await never.wait()
        return None

    gateway = _gateway(handler, tmp_path, max_replies={"c2c": 4})
    await gateway.start()
    api, ws = FakeApi.instances[0], FakeWebSocket.instances[0]
    api.hold = {"进度 1": never}
    handling = asyncio.create_task(ws.callbacks.on_message_event("C2C_MESSAGE_CREATE", C2C_EVENT))
    await asyncio.sleep(0.01)
    handling.cancel()  # 进程退出时，还在处理的消息会被取消
    await asyncio.wait([handling])
    await asyncio.sleep(0)
    await gateway.stop()
    assert api.sent == [] and asyncio.all_tasks() == {asyncio.current_task()}


# 消息里一张图片附件的形状（字段和 SDK 的 MessageAttachment 一一对应）
IMAGE = {
    "content_type": "image/png",
    "filename": "5A0F.png",
    "height": 1024,
    "width": 768,
    "size": 34567,
    "url": "https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=abc&rkey=xyz",
}


async def test_images_are_passed_on_and_other_attachments_are_ignored(
    sdk: None, tmp_path: Path
) -> None:
    received: list[InboundMessage] = []

    async def handler(message: InboundMessage) -> OutboundMessage | None:
        received.append(message)
        return None

    gateway = _gateway(handler, tmp_path)
    await gateway.start()
    ws = FakeWebSocket.instances[0]
    voice = {"content_type": "voice", "filename": "a.amr", "url": "https://qq.example/a.amr"}
    untyped = {"filename": "B.JPG", "url": "//gchat.qpic.cn/b"}  # 没写类型：看扩展名
    # 单聊：一句话带图片和语音
    event = {**C2C_EVENT, "content": "这是哪个皮肤", "attachments": [IMAGE, voice, untyped]}
    await ws.callbacks.on_message_event("C2C_MESSAGE_CREATE", event)
    # 群聊：@机器人只发了一张图
    event = {**GROUP_EVENT, "content": " ", "attachments": [IMAGE]}
    await ws.callbacks.on_message_event("GROUP_AT_MESSAGE_CREATE", event)
    # 只有语音，或者什么都没有：没什么可回的，只交给旁听的插件看
    await ws.callbacks.on_message_event(
        "C2C_MESSAGE_CREATE", {**C2C_EVENT, "content": "", "attachments": [voice]}
    )
    await ws.callbacks.on_message_event("C2C_MESSAGE_CREATE", {**C2C_EVENT, "content": ""})
    await gateway.stop()

    assert [message.addressed for message in received] == [True, True, False, False]
    with_text, image_only = received[:2]
    assert with_text.content == "这是哪个皮肤"
    assert with_text.images == [
        ImageRef(
            url=str(IMAGE["url"]),
            content_type="image/png",
            width=768,
            height=1024,
            size=34567,
            filename="5A0F.png",
        ),
        ImageRef(url="https://gchat.qpic.cn/b", filename="B.JPG"),
    ]
    assert (image_only.chat_scope, image_only.content) == (ChatScope.GROUP, "")
    assert [image.url for image in image_only.images] == [IMAGE["url"]]


# 群主把「机器人可获取的群聊消息范围」设成全部消息之后，这个群的消息改用 GROUP_MESSAGE_CREATE 推：
# 字段和 @ 消息的事件一样，@ 写成 <@成员标识>，被 @ 的人列在 mentions 里，机器人自己那一项带 is_you
FULL_EVENT = {
    "id": "ROBOT1.0_full",
    "author": {
        "id": "MEMBER",
        "member_openid": "MEMBER",
        "member_role": "member",
        "username": "小明",
    },
    "content": "<@BOTID> /查 山风",
    "group_openid": "GROUP",
    "message_type": 0,
    "timestamp": "2026-07-21T10:00:00+08:00",
    "mentions": [
        {"id": "BOTID", "member_openid": "BOTID", "username": "扫地工", "bot": True, "is_you": True}
    ],
}


async def test_in_a_group_that_sends_everything_only_mentions_of_the_bot_are_answered(
    sdk: None, tmp_path: Path
) -> None:
    received: list[InboundMessage] = []

    async def handler(message: InboundMessage) -> OutboundMessage | None:
        received.append(message)
        return OutboundMessage(content="收到", reply_to=message.message_id)

    gateway = _gateway(handler, tmp_path)
    await gateway.start()
    api, ws = FakeApi.instances[0], FakeWebSocket.instances[0]
    chatter = {
        **FULL_EVENT,
        "id": "m-chat",
        "content": "<@OTHER> 早上好 <@all>",
        "mentions": [{"id": "OTHER", "username": "阿岚", "bot": False}],
    }
    another_bot = {
        **FULL_EVENT,
        "id": "m-bot",
        "content": "<@BOT2> 签到",
        "mentions": [{"id": "BOT2", "username": "别的机器人", "bot": True}],
    }
    for event in (
        FULL_EVENT,
        chatter,
        another_bot,
        {**FULL_EVENT, "id": "m-plain", "content": "大家早上好", "mentions": []},
    ):
        await ws.callbacks.on_message_event("GROUP_MESSAGE_CREATE", event)
    await gateway.stop()

    # 每一条都交给处理方（旁听的插件要记），但只有 @ 了机器人的那条算是对它说的
    assert [(m.content, m.addressed) for m in received] == [
        ("/查 山风", True),  # @ 机器人的标记去掉了，和只推 @ 消息时一样
        ("@阿岚 早上好 @全体成员", False),  # @ 别人的换成昵称
        ("@别的机器人 签到", False),  # @ 的是别的机器人：不是 is_you 就不算
        ("大家早上好", False),
    ]
    assert {(m.chat_scope, m.chat_id, m.user_id, m.user_name) for m in received} == {
        (ChatScope.GROUP, "GROUP", "MEMBER", "小明")
    }
    # 不是对机器人说的，处理方就算给了回复也不发
    assert [(s["reply_to"], s["content"]) for s in api.sent] == [("ROBOT1.0_full", "收到")]


async def test_events_the_sdk_does_not_know_are_delivered_or_logged() -> None:
    from qqbot_agent_sdk import QQWebSocket, WSCallbacks

    from jianshanagent.gateway.qq import socket_class

    # 用到的 SDK 内部方法都还在（升级 SDK 后少了哪个，这里先报出来）
    for name in ("_dispatch_payload", "_handle_dispatch", "_is_duplicate", "_dispatch_to_main"):
        assert callable(getattr(QQWebSocket, name))

    seen: list[tuple[str, str]] = []
    clicks: list[dict[str, Any]] = []

    async def on_message_event(event_type: str, raw: dict[str, Any]) -> None:
        seen.append((event_type, raw["id"]))

    async def on_interaction_event(event_type: str, raw: dict[str, Any]) -> None:
        clicks.append(raw)

    unused: Any = lambda *args: None  # noqa: E731
    callbacks = WSCallbacks(
        on_message_event=on_message_event,
        on_interaction_event=on_interaction_event,
        on_connected=unused,
        on_disconnected=unused,
        on_fatal_error=unused,
        get_token=lambda: "token",
        get_session=lambda: (None, None),
        set_session=unused,
        set_heartbeat_interval=unused,
        clear_token=unused,
        fail_pending=unused,
        get_gateway_url=lambda: "wss://gateway.example",
    )
    socket = socket_class(QQWebSocket)(callbacks=callbacks, log_tag="test")
    assert isinstance(socket, QQWebSocket)
    # 网关靠这个属性看连接线程还在不在（没启动时是 None）
    assert hasattr(socket, "_ws_thread") and socket._ws_thread is None

    socket._handle_dispatch("GROUP_MESSAGE_CREATE", FULL_EVENT)
    socket._handle_dispatch("GROUP_MESSAGE_CREATE", FULL_EVENT)  # 平台重发的同一条：只处理一次
    socket._handle_dispatch("GROUP_AT_MESSAGE_CREATE", GROUP_EVENT)  # SDK 认识的照旧
    socket._handle_dispatch("GROUP_DEL_ROBOT", {"group_openid": "GROUP"})  # 不认识的只记日志
    # 按钮点击：平台发来的整个事件。回复要用最外层的 id，SDK 只把里面的 d 传给回调
    click = {key: value for key, value in CLICK_EVENT.items() if key != "_envelope_id"}
    frame = {"op": 0, "s": 7, "t": "INTERACTION_CREATE", "id": "INTERACTION_CREATE:e1", "d": click}
    socket._dispatch_payload(frame)
    socket._dispatch_payload({**frame, "s": 8})  # 平台重发的同一次点击：只处理一次
    await asyncio.sleep(0)

    assert seen == [
        ("GROUP_MESSAGE_CREATE", "ROBOT1.0_full"),
        ("GROUP_AT_MESSAGE_CREATE", "ROBOT1.0_msg"),
    ]
    assert clicks == [{**click, "_envelope_id": "INTERACTION_CREATE:e1"}]


async def test_session_is_kept_so_a_dropped_connection_or_a_restart_can_resume(
    sdk: None, tmp_path: Path
) -> None:
    async def handler(_message: InboundMessage) -> OutboundMessage | None:
        return None

    gateway = _gateway(handler, tmp_path)
    await gateway.start()
    callbacks = FakeWebSocket.instances[0].callbacks
    assert callbacks.get_session() == (None, None)  # 第一次启动：没有会话，要重新登录

    # SDK 的做法：每收到一个事件先问现在的会话，再把序号写回去；登录成功时再把会话号写进来
    callbacks.set_session(callbacks.get_session()[0], 1)
    callbacks.set_session("session-1", callbacks.get_session()[1])
    callbacks.set_session(callbacks.get_session()[0], 2)
    assert callbacks.get_session() == ("session-1", 2)  # 断线后拿它续接
    await gateway.stop()

    # 重启：接着上次的会话和序号
    FakeWebSocket.instances.clear()
    restarted = _gateway(handler, tmp_path)
    await restarted.start()
    callbacks = FakeWebSocket.instances[0].callbacks
    assert callbacks.get_session() == ("session-1", 2)

    # 平台说会话作废了：清掉，下次重新登录；重启后也不再拿作废的会话去续接
    callbacks.set_session(None, None)
    assert callbacks.get_session() == (None, None)
    await restarted.stop()
    FakeWebSocket.instances.clear()
    again = _gateway(handler, tmp_path)
    await again.start()
    assert FakeWebSocket.instances[0].callbacks.get_session() == (None, None)
    await again.stop()


async def test_stopping_waits_for_messages_in_progress_and_tells_the_rest_about_the_restart(
    sdk: None, tmp_path: Path
) -> None:
    release = asyncio.Event()
    started: list[str] = []

    async def handler(message: InboundMessage) -> OutboundMessage | None:
        started.append(message.message_id)
        await message.progress("正在查资料…")
        if message.message_id == "quick":
            await release.wait()
        else:
            await asyncio.sleep(3600)  # 这一条到进程退出都答不完
        return OutboundMessage(content="结论", reply_to=message.message_id)

    gateway = _gateway(handler, tmp_path, max_replies={"c2c": 4}, drain_seconds=0.2)
    await gateway.start()
    api, ws = FakeApi.instances[0], FakeWebSocket.instances[0]
    tasks = [
        asyncio.create_task(
            ws.callbacks.on_message_event("C2C_MESSAGE_CREATE", {**C2C_EVENT, "id": name})
        )
        for name in ("quick", "slow")
    ]
    while len(started) < 2:
        await asyncio.sleep(0)

    # 发布、重启时进程收到退出的信号：先不再收新消息，等正在处理的答完
    stopping = asyncio.create_task(gateway.stop())
    await asyncio.sleep(0.01)
    assert ws.stopped is True and not stopping.done() and not api.http.is_closed
    release.set()
    await stopping
    await asyncio.gather(*tasks, return_exceptions=True)

    finals = {s["reply_to"]: s["content"] for s in api.sent if s["content"] != "正在查资料…"}
    # 来得及的那条照常答完；等不到的那条也有个交代，不是看到「正在查资料…」就再没有下文
    assert finals == {
        "quick": "结论",
        "slow": "机器人正在重启，这个问题没来得及处理完，请过一会儿再问一次。",
    }
    assert api.http.is_closed  # 回复都发完了才关的


async def test_a_dead_connection_is_reconnected_and_shows_in_the_health_check(
    sdk: None, tmp_path: Path
) -> None:
    async def handler(_message: InboundMessage) -> OutboundMessage | None:
        return None

    async def settle() -> None:
        await asyncio.sleep(0.05)

    gateway = _gateway(handler, tmp_path, watch_seconds=0.005, revive_delays=(0,))
    await gateway.start()
    first = FakeWebSocket.instances[0]
    assert gateway.health() == (True, "")  # 刚启动、还在连：不算有问题
    first.callbacks.on_connected()
    await settle()
    assert len(FakeWebSocket.instances) == 1 and gateway.health() == (True, "")

    # 断开了、SDK 自己在重连（线程还在）：不插手；断得久了健康检查报出来
    first.callbacks.on_disconnected()
    await settle()
    assert len(FakeWebSocket.instances) == 1 and gateway.health()[0] is True
    assert gateway.down_since is not None
    gateway.down_since -= 1000
    ok, problem = gateway.health()
    assert ok is False and "断开" in problem

    # 连接线程没了（启动时第一次握手就失败，SDK 这时不重试也不通知）：重新连一条
    assert first._ws_thread is not None
    first._ws_thread.alive = False
    await settle()
    assert len(FakeWebSocket.instances) == 2 and first.stopped is True
    second = FakeWebSocket.instances[1]
    assert second.started_with == "wss://gateway.example"
    second.callbacks.on_connected()
    assert gateway.health() == (True, "")

    # SDK 重连了太多次自己放弃了：过一阵再连一条
    second.callbacks.on_fatal_error("qq_max_reconnect", "Max reconnect attempts reached")
    await settle()
    assert len(FakeWebSocket.instances) == 3
    third = FakeWebSocket.instances[2]
    third.callbacks.on_connected()

    # 平台不让连（封禁、下架、没有权限）：重连没有用，不去碰它，健康检查报出来等人处理
    third.callbacks.on_fatal_error("qq_banned", "Bot is banned")
    await settle()
    assert len(FakeWebSocket.instances) == 3
    ok, problem = gateway.health()
    assert ok is False and "qq_banned" in problem
    await gateway.stop()


def test_only_one_to_one_chats_share_a_session_without_the_speaker() -> None:
    def key(scope: ChatScope) -> str:
        message = InboundMessage(
            message_id="m", chat_scope=scope, chat_id="CHAT", user_id="USER", content="x"
        )
        return message.session_key

    # 对话历史、记忆、限流都按会话键分开。一对一的会话就是会话本身
    assert (key(ChatScope.C2C), key(ChatScope.DM)) == ("c2c:CHAT", "dm:CHAT")
    # 多人的会话要带上发言人：不然同一个子频道里所有人共用一份记忆，一个人还能把别人的删掉
    assert key(ChatScope.GROUP) == "group:CHAT:user:USER"
    assert key(ChatScope.GUILD) == "guild:CHAT:user:USER"


def test_missing_credentials_are_rejected() -> None:
    with pytest.raises(ValueError, match="AppID"):
        QQGateway(app_id="", client_secret="", on_message=lambda m: None)  # type: ignore[arg-type, return-value]
