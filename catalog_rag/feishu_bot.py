"""Feishu (飞书) bot over the long-connection event channel: no public URL needed.

Handles ``im.message.receive_v1`` for private chats and group messages that @mention the bot, ignores messages sent
by apps (including itself), de-duplicates event retries, answers in a worker thread (the event callback must return
within a few seconds) and replies to the original message in the same chat.

Roles: ``FEISHU_USER_ROLES`` maps open_ids to roles ("ou_a:销售,ou_b:客服"); everyone else gets
``FEISHU_DEFAULT_ROLE`` (default 客服, least privilege). ``FEISHU_ALLOW_ROLE_TAG=1`` lets a message start with
``#销售`` / ``#客服`` to switch role — for demos only. Filters can be written inline: ``材质:316 价格:0-150``.
"""
from __future__ import annotations

import json
import re
import threading
import time
from collections import OrderedDict
from concurrent.futures import Executor, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Protocol

from .assistant import Answer
from .catalog import Filters
from .config import ROLES, Settings

MAX_REPLY_CHARS = 3500
HELP = ("你好，我是产品目录助手（演示数据均为虚构）。直接提问即可，例如：\n"
        "· 6204-2RS 的外径是多少？\n· 25×3 的 NBR O型圈耐温不够，换哪个？\n"
        "· 给沪东衡砺报个价：6204-2RS 200个（仅销售角色）\n"
        "可加筛选：材质:316 尺寸:DN25 型号:LJ18 价格:0-150（价格筛选仅销售）")
_MENTION = re.compile(r"@_user_\d+")
_FILTER = re.compile(r"(材质|尺寸|型号|价格)[:：]\s*(\S+)")
_ROLE_TAG = re.compile(r"^#(销售|客服)\s*")


@dataclass
class IncomingMessage:
    event_id: str
    message_id: str
    chat_type: str            # "p2p" | "group"
    message_type: str         # "text", "image", ...
    content: str              # raw JSON content of the message
    sender_type: str          # "user" | "app"
    sender_open_id: str
    mention_count: int = 0


@dataclass
class ParsedCommand:
    question: str
    role_tag: str | None = None
    filters: Filters = field(default_factory=Filters)


def parse_text(text: str, allow_role_tag: bool) -> ParsedCommand:
    """Strip @mentions, read an optional role tag and inline filters; the rest is the question."""
    text = _MENTION.sub("", text).strip()
    role_tag = None
    match = _ROLE_TAG.match(text)
    if match and allow_role_tag:
        role_tag = match.group(1)
        text = text[match.end():]
    filters = Filters()
    for key, value in _FILTER.findall(text):
        if key == "材质":
            filters.material = value
        elif key == "尺寸":
            filters.size = value
        elif key == "型号":
            filters.model = value
        else:
            low, _, high = value.partition("-")
            filters.price_min = float(low) if _is_number(low) else None
            filters.price_max = float(high) if _is_number(high) else None
    return ParsedCommand(_FILTER.sub("", text).strip(), role_tag, filters)


def _is_number(value: str) -> bool:
    return bool(re.fullmatch(r"\d+(?:\.\d+)?", value))


def parse_role_map(spec: str) -> dict[str, str]:
    roles = {}
    for item in filter(None, (s.strip() for s in spec.split(","))):
        open_id, _, role = item.partition(":")
        if role in ROLES:
            roles[open_id.strip()] = role
    return roles


class Deduper:
    """Remembers recently seen event / message ids (Feishu re-delivers events that were not acknowledged in time)."""

    def __init__(self, capacity: int = 2048, ttl_seconds: float = 3600):
        self.capacity, self.ttl = capacity, ttl_seconds
        self._seen: OrderedDict[str, float] = OrderedDict()
        self._lock = threading.Lock()

    def seen(self, *keys: str) -> bool:
        now = time.monotonic()
        with self._lock:
            while self._seen and next(iter(self._seen.values())) < now - self.ttl:
                self._seen.popitem(last=False)
            duplicate = any(k in self._seen for k in keys if k)
            for k in keys:
                if k:
                    self._seen[k] = now
                    self._seen.move_to_end(k)
            while len(self._seen) > self.capacity:
                self._seen.popitem(last=False)
            return duplicate


class Replier(Protocol):
    def reply(self, message_id: str, text: str) -> bool: ...


AskFn = Callable[[str, str, Filters, str], Answer]   # (question, role, filters, user) -> Answer


class FeishuBot:
    def __init__(self, ask: AskFn, replier: Replier, settings: Settings, executor: Executor,
                 status: Callable[[str], None] = print):
        self.ask, self.replier, self.settings, self.executor, self.status = ask, replier, settings, executor, status
        self.roles = parse_role_map(settings.feishu_user_roles)
        self.deduper = Deduper()

    def on_message(self, msg: IncomingMessage) -> None:
        """Called on the SDK's event thread: filter, de-duplicate, then hand off to a worker and return."""
        if msg.sender_type != "user":
            return
        if self.deduper.seen(msg.event_id, msg.message_id):
            self.status("忽略重复投递的事件")
            return
        if msg.chat_type == "group" and msg.mention_count == 0:
            return
        if msg.message_type != "text":
            self.executor.submit(self.replier.reply, msg.message_id, "目前只支持文字消息。")
            return
        try:
            text = json.loads(msg.content).get("text", "")
        except json.JSONDecodeError:
            text = ""
        command = parse_text(text, self.settings.feishu_allow_role_tag)
        if not command.question or command.question in ("帮助", "help", "/help"):
            self.executor.submit(self.replier.reply, msg.message_id, HELP)
            return
        role = command.role_tag or self.roles.get(msg.sender_open_id, self.settings.feishu_default_role)
        self.executor.submit(self._answer_and_reply, msg, command, role)

    def _answer_and_reply(self, msg: IncomingMessage, command: ParsedCommand, role: str) -> None:
        start = time.perf_counter()
        try:
            answer = self.ask(command.question, role, command.filters, msg.sender_open_id)
            text, status = answer.render(), answer.status
        except Exception as exc:  # noqa: BLE001 - the user must always get a reply
            text, status = f"处理失败：{type(exc).__name__}", "error"
        header = f"（角色：{role}）\n"
        sent = self.replier.reply(msg.message_id, (header + text)[:MAX_REPLY_CHARS])
        self.status(f"{msg.chat_type} 消息 → {status}，{'已回复' if sent else '回复失败'}，"
                    f"{time.perf_counter() - start:.1f}s")


# --------------------------------------------------------------------------- lark SDK glue


class LarkReplier:
    def __init__(self, app_id: str, app_secret: str, status: Callable[[str], None] = print):
        import lark_oapi as lark

        self._client = lark.Client.builder().app_id(app_id).app_secret(app_secret) \
            .log_level(lark.LogLevel.WARNING).build()
        self.status = status

    def reply(self, message_id: str, text: str) -> bool:
        from lark_oapi.api.im.v1 import ReplyMessageRequest, ReplyMessageRequestBody

        body = ReplyMessageRequestBody.builder().msg_type("text") \
            .content(json.dumps({"text": text}, ensure_ascii=False)).build()
        response = self._client.im.v1.message.reply(
            ReplyMessageRequest.builder().message_id(message_id).request_body(body).build())
        if not response.success():
            self.status(f"飞书回复失败：code={response.code} msg={response.msg}")
        return bool(response.success())


def to_incoming(data) -> IncomingMessage:  # noqa: ANN001 - lark_oapi P2ImMessageReceiveV1
    event, header = data.event, data.header
    sender_id = event.sender.sender_id if event.sender else None
    return IncomingMessage(
        event_id=header.event_id if header else "",
        message_id=event.message.message_id or "",
        chat_type=event.message.chat_type or "",
        message_type=event.message.message_type or "",
        content=event.message.content or "",
        sender_type=(event.sender.sender_type if event.sender else "") or "",
        sender_open_id=(sender_id.open_id if sender_id else "") or "",
        mention_count=len(event.message.mentions or []),
    )


def run_bot(settings: Settings) -> None:
    import lark_oapi as lark

    from .runtime import build_assistant

    if not (settings.feishu_app_id and settings.feishu_app_secret):
        raise SystemExit("需要环境变量 FEISHU_APP_ID 和 FEISHU_APP_SECRET")
    assistant = build_assistant(settings)

    def ask(question: str, role: str, filters: Filters, user: str) -> Answer:
        return assistant.ask(question, role, filters, channel="feishu", user=user)

    def status(line: str) -> None:
        print(f"[{time.strftime('%H:%M:%S')}] {line}", flush=True)

    bot = FeishuBot(ask, LarkReplier(settings.feishu_app_id, settings.feishu_app_secret, status), settings,
                    ThreadPoolExecutor(max_workers=4), status)
    handler = lark.EventDispatcherHandler.builder("", "") \
        .register_p2_im_message_receive_v1(lambda data: bot.on_message(to_incoming(data))).build()
    client = lark.ws.Client(settings.feishu_app_id, settings.feishu_app_secret, event_handler=handler,
                            log_level=lark.LogLevel.WARNING)
    status(f"飞书机器人已启动（长连接；{settings.label}；默认角色 {settings.feishu_default_role}）。Ctrl+C 退出")
    client.start()
