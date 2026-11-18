"""Feishu bot logic without the SDK connection: filtering, de-duplication, roles, parsing and replies."""
from __future__ import annotations

import json
import unittest
from concurrent.futures import Executor, Future
from dataclasses import replace
from pathlib import Path

from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

from catalog_rag.assistant import Answer
from catalog_rag.catalog import Filters
from catalog_rag.feishu_bot import Deduper, FeishuBot, IncomingMessage, parse_role_map, parse_text, to_incoming
from tests.support import settings


class SyncExecutor(Executor):
    def submit(self, fn, /, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        future: Future = Future()
        future.set_result(fn(*args, **kwargs))
        return future


class FakeReplier:
    def __init__(self):
        self.replies: list[tuple[str, str]] = []

    def reply(self, message_id: str, text: str) -> bool:
        self.replies.append((message_id, text))
        return True


def message(text: str = "6204-2RS 外径？", **overrides) -> IncomingMessage:  # noqa: ANN003
    base = dict(event_id="ev1", message_id="om1", chat_type="p2p", message_type="text",
                content=json.dumps({"text": text}, ensure_ascii=False), sender_type="user",
                sender_open_id="ou_owner", mention_count=0)
    base.update(overrides)
    return IncomingMessage(**base)


class ParseTest(unittest.TestCase):
    def test_mentions_filters_and_role_tag(self):
        cmd = parse_text("@_user_1 #客服 有哪些球阀 材质:316 价格:0-150", allow_role_tag=True)
        self.assertEqual((cmd.question, cmd.role_tag), ("有哪些球阀", "客服"))
        self.assertEqual((cmd.filters.material, cmd.filters.price_min, cmd.filters.price_max), ("316", 0.0, 150.0))
        cmd = parse_text("#客服 尺寸：M6 有哪些螺钉", allow_role_tag=False)
        self.assertIsNone(cmd.role_tag)
        self.assertEqual((cmd.filters.size, cmd.question), ("M6", "#客服  有哪些螺钉"))
        self.assertEqual(parse_text("价格:-200 球阀", False).filters.price_max, 200.0)

    def test_role_map_ignores_unknown_roles(self):
        self.assertEqual(parse_role_map("ou_a:销售, ou_b:老板,ou_c:客服"), {"ou_a": "销售", "ou_c": "客服"})

    def test_deduper(self):
        d = Deduper(capacity=10)
        self.assertFalse(d.seen("a", "m1"))
        self.assertTrue(d.seen("b", "m1"))      # same message re-delivered under a new event id
        self.assertTrue(d.seen("a"))

    def test_sdk_event_conversion(self):
        event = P2ImMessageReceiveV1({
            "schema": "2.0", "header": {"event_id": "ev9", "event_type": "im.message.receive_v1"},
            "event": {"sender": {"sender_id": {"open_id": "ou_x"}, "sender_type": "user"},
                      "message": {"message_id": "om9", "chat_type": "group", "message_type": "text",
                                  "content": "{\"text\": \"@_user_1 hi\"}",
                                  "mentions": [{"key": "@_user_1", "name": "bot"}]}}})
        msg = to_incoming(event)
        self.assertEqual((msg.event_id, msg.message_id, msg.chat_type, msg.sender_open_id, msg.mention_count),
                         ("ev9", "om9", "group", "ou_x", 1))


class BotTest(unittest.TestCase):
    def setUp(self):
        self.asked: list[tuple[str, str, Filters, str]] = []
        self.replier = FakeReplier()
        self.status: list[str] = []

    def bot(self, roles: str = "", allow_tag: bool = False, fail: bool = False) -> FeishuBot:
        def ask(question: str, role: str, filters: Filters, user: str) -> Answer:
            if fail:
                raise RuntimeError("boom")
            self.asked.append((question, role, filters, user))
            return Answer(question, role, "qa", "answered", "外径 47mm")

        s = replace(settings(Path(".")), feishu_user_roles=roles, feishu_allow_role_tag=allow_tag)
        return FeishuBot(ask, self.replier, s, SyncExecutor(), self.status.append)

    def test_answers_and_replies_in_the_same_thread(self):
        self.bot().on_message(message())
        self.assertEqual(self.asked[0][:2], ("6204-2RS 外径？", "客服"))   # least-privilege default role
        mid, text = self.replier.replies[0]
        self.assertEqual(mid, "om1")
        self.assertTrue(text.startswith("（角色：客服）") and "外径 47mm" in text)
        self.assertIn("已回复", self.status[-1])

    def test_role_comes_from_the_map_or_an_allowed_tag(self):
        self.bot(roles="ou_owner:销售").on_message(message())
        self.bot(allow_tag=True).on_message(message("#销售 6204 价格", event_id="ev2", message_id="om2"))
        self.assertEqual([a[1] for a in self.asked], ["销售", "销售"])

    def test_ignores_apps_duplicates_and_unmentioned_group_messages(self):
        bot = self.bot()
        bot.on_message(message(sender_type="app"))
        bot.on_message(message(chat_type="group", mention_count=0))
        bot.on_message(message(event_id="ev5", message_id="om5"))
        bot.on_message(message(event_id="ev5", message_id="om5"))
        self.assertEqual(len(self.asked), 1)
        self.assertIn("重复", self.status[-1])

    def test_non_text_help_and_failures_still_get_a_reply(self):
        self.bot().on_message(message(message_type="image", content="{}"))
        self.bot().on_message(message("@_user_1 ", event_id="e2", message_id="m2"))
        self.bot(fail=True).on_message(message(event_id="e3", message_id="m3"))
        texts = [t for _, t in self.replier.replies]
        self.assertIn("只支持文字", texts[0])
        self.assertIn("产品目录助手", texts[1])
        self.assertIn("处理失败：RuntimeError", texts[2])


if __name__ == "__main__":
    unittest.main()
