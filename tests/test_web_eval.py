"""Web endpoints (with a fake model) and the evaluator's scoring rules; also sanity-checks the gold set."""
from __future__ import annotations

import json
import unittest
from urllib.parse import quote

from fastapi.testclient import TestClient

from catalog_rag.assistant import Answer, Citation
from catalog_rag.evaluate import score_question, summarize
from catalog_rag.retrieval import Hit
from catalog_rag.web import create_app
from tests.support import DATA, FakeTransport, answer_json, assistant, index


class WebTest(unittest.TestCase):
    def client(self, *chat: str) -> TestClient:
        a = assistant(self.addCleanup, FakeTransport(*chat))
        return TestClient(create_app(a.settings, a))

    def test_page_info_and_ask(self):
        c = self.client(answer_json("外径 47mm [1]", citations=[1]))
        self.assertIn("演示数据均为虚构", c.get("/").text)
        self.assertEqual(c.get("/api/info").json()["products"], 300)
        body = c.post("/api/ask", json={"question": "6204-2RS 外径", "role": "销售", "filters": {"material": ""}}).json()
        self.assertEqual(body["status"], "answered")
        self.assertEqual(body["citations"][0]["n"], 1)
        self.assertEqual(c.get("/api/logs").json()[0]["status"], "answered")
        self.assertEqual(c.post("/api/ask", json={"question": "x", "role": "老板"}).status_code, 400)

    def test_documents_and_chunks_respect_roles_and_paths(self):
        c = self.client()
        self.assertEqual(c.get("/files/" + quote("RF-01_深沟球轴承规格书.pdf")).status_code, 200)
        self.assertEqual(c.get("/files/" + quote("瑞丰价目表_2026Q4.xlsx") + "?role=客服").status_code, 404)
        self.assertEqual(c.get("/files/" + quote("瑞丰价目表_2026Q4.xlsx") + "?role=销售").status_code, 200)
        self.assertEqual(c.get("/files/..%2Fgold_set.json").status_code, 404)
        price_chunk = next(ch.id for ch in index().chunks if ch.sales_only)
        self.assertEqual(c.get(f"/api/chunk/{quote(price_chunk)}?role=客服").status_code, 404)
        self.assertEqual(c.get(f"/api/chunk/{quote(price_chunk)}?role=销售").status_code, 200)
        self.assertEqual(c.get("/quotes/..%2F..%2Fdata%2Fgold_set.json").status_code, 404)


class EvaluateTest(unittest.TestCase):
    def answer(self, status: str, text: str = "", cited: tuple[str, ...] = (), hit: str | None = None) -> Answer:
        by_key = {c.source_key: c for c in index().chunks}
        a = Answer("q", "客服", "qa", status, text)
        a.citations = [Citation(i + 1, by_key[k].id, by_key[k].doc, by_key[k].locator, "") for i, k in enumerate(cited)]
        if hit:
            a.hits = [Hit(by_key[hit], 1.0)]
        return a

    def test_scoring_rules(self):
        src = "RF-01_深沟球轴承规格书.pdf|p2"
        q = {"id": "A1", "category": "c", "role": "客服", "question": "q", "expect": "answer", "sources": [src],
             "must_include": ["47", ["14", "十四"]], "must_not_include": ["6305"]}
        good = score_question(q, self.answer("answered", "外径 47，宽 14 [1]", (src,), src))
        self.assertTrue(good["correct"] and good["hit5"] and good["citation_ok"])
        bad = score_question(q, self.answer("answered", "外径 47，6305 也行", (), src))
        self.assertFalse(bad["correct"] or bad["citation_ok"])
        self.assertIn("缺少：14/十四", bad["reason"])
        refuse = {**q, "expect": "refuse", "sources": []}
        self.assertTrue(score_question(refuse, self.answer("refused"))["correct"])
        self.assertFalse(score_question(refuse, self.answer("answered", "x"))["correct"])
        rows = [good, bad, score_question(refuse, self.answer("refused"))]
        s = summarize(rows)
        self.assertEqual((s["answer_correct"]["n"], s["answer_correct"]["d"]), (1, 2))
        self.assertEqual(s["refusal_correct"]["pct"], 100.0)

    def test_gold_set_is_well_formed_and_points_at_real_sources(self):
        gold = json.loads((DATA / "gold_set.json").read_text(encoding="utf-8"))["questions"]
        keys = {c.source_key for c in index().chunks}
        self.assertEqual(len(gold), 50)
        self.assertEqual(len({q["id"] for q in gold}), 50)
        expects = [q["expect"] for q in gold]
        self.assertEqual((expects.count("answer"), expects.count("refuse"), expects.count("deny")), (40, 7, 3))
        for q in gold:
            for source in q["sources"]:
                self.assertIn(source, keys, q["id"])


if __name__ == "__main__":
    unittest.main()
