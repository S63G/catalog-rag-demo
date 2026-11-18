"""Retrieval pipelines, permissions, citations, refusals, substitutes, quotes and the query log."""
from __future__ import annotations

import json
import sqlite3
import unittest
from contextlib import closing

from catalog_rag.catalog import Filters
from catalog_rag.retrieval import _rrf
from catalog_rag.tracing import Trace
from tests.support import FakeTransport, answer_json, assistant, index


def all_ids(sales: bool = True) -> set[str]:
    return {c.id for c in index().chunks if sales or not c.sales_only}


class RetrievalTest(unittest.TestCase):
    def search(self, pipeline, query, allowed=None, k=5, transport=None):  # noqa: ANN001
        transport = transport or FakeTransport()
        a = assistant(self.addCleanup, transport, pipeline)
        return a.retriever.search(query, allowed if allowed is not None else all_ids(), k, Trace()), transport

    def test_bm25_finds_the_exact_table_row(self):
        (hits, warnings), transport = self.search("bm25", "6204-2RS 的外径")
        self.assertIn("1.01.0009", hits[0].chunk.skus)
        self.assertEqual((warnings, transport.calls), ([], []))

    def test_allow_list_is_applied_before_ranking(self):
        (hits, _), _ = self.search("hybrid_rerank", "6204-2RS 目录含税单价", allowed=all_ids(sales=False))
        self.assertTrue(hits)
        self.assertFalse(any(h.chunk.sales_only for h in hits))
        (hits, _), _ = self.search("bm25", "6204-2RS", allowed=set())
        self.assertEqual(hits, [])

    def test_rerank_decides_the_final_order(self):
        transport = FakeTransport(rerank=lambda q, docs: [float(i) for i in range(len(docs))])  # reverse order
        (hits, _), _ = self.search("hybrid_rerank", "O型圈 FKM 耐温", transport=transport)
        self.assertEqual([h.ranks["rerank"] for h in hits], [1, 2, 3, 4, 5])
        self.assertTrue(all("bm25" in h.ranks or "vector" in h.ranks for h in hits))
        self.assertEqual(transport.count("rerank"), 1)

    def test_provider_failures_degrade_with_a_warning(self):
        (hits, warnings), _ = self.search("hybrid_rerank", "6204-2RS", transport=FakeTransport(fail={"embeddings"}))
        self.assertTrue(hits and "降级为 BM25" in warnings[0])
        (hits, warnings), _ = self.search("hybrid_rerank", "6204-2RS", transport=FakeTransport(fail={"rerank"}))
        self.assertTrue(hits and "未重排" in warnings[0])

    def test_reciprocal_rank_fusion(self):
        fused = _rrf({"bm25": [(1, 9.0), (2, 8.0)], "vector": [(2, 0.9), (3, 0.8)]})
        self.assertEqual([i for i, _, _ in fused], [2, 1, 3])
        self.assertEqual(fused[0][2], {"bm25": 2, "vector": 1})


class PermissionTest(unittest.TestCase):
    def test_support_cannot_ask_prices_or_quotes(self):
        transport = FakeTransport()
        a = assistant(self.addCleanup, transport)
        for question, filters in (("6204-ZZ 多少钱一个？", None), ("给沪东衡砺报个价：6204-2RS 10个", None),
                                  ("有哪些球阀", Filters(price_max=100))):
            answer = a.ask(question, "客服", filters)
            self.assertEqual(answer.status, "denied", question)
        self.assertEqual(transport.calls, [])

    def test_support_context_never_contains_price_rows(self):
        transport = FakeTransport(answer_json("见资料 [1]", citations=[1]))
        a = assistant(self.addCleanup, transport)
        a.ask("6204-2RS 的价目表信息", "客服")
        prompt = transport.calls[-1][1]["messages"][1]["content"]
        self.assertNotIn("目录含税单价", prompt)
        self.assertIn("客服看不到价格", transport.calls[-1][1]["messages"][0]["content"])

    def test_role_is_validated(self):
        with self.assertRaises(ValueError):
            assistant(self.addCleanup, FakeTransport()).ask("hi", "老板")


class AnswerTest(unittest.TestCase):
    def ask(self, *responses: str, question: str = "6204-2RS 的外径是多少？", role: str = "销售",
            filters: Filters | None = None):
        transport = FakeTransport(*responses)
        a = assistant(self.addCleanup, transport)
        return a.ask(question, role, filters, user="ou_secret_user"), transport, a

    def test_citations_map_to_chunks_and_bad_markers_are_removed(self):
        answer, _, _ = self.ask(answer_json("外径 47mm，宽度 14mm [1][9]", citations=[1]))
        self.assertEqual(answer.status, "answered")
        self.assertEqual([c.n for c in answer.citations], [1])
        self.assertEqual(answer.citations[0].chunk_id, answer.hits[0].chunk.id)
        self.assertNotIn("[9]", answer.text)
        self.assertTrue(any("[9]" in w for w in answer.warnings))
        self.assertIn("来源：", answer.render())

    def test_refusal_has_no_citations(self):
        answer, _, _ = self.ask(answer_json("目录资料中没有额定动载荷的信息", answerable=False, citations=[1]))
        self.assertEqual((answer.status, answer.citations), ("refused", []))

    def test_uncited_and_ungrounded_answers_are_flagged(self):
        answer, _, _ = self.ask(answer_json("外径 47mm，动载荷 12345N"))
        self.assertTrue(any("没有标注来源" in w for w in answer.warnings))
        self.assertTrue(any("12345" in w for w in answer.warnings))

    def test_support_answers_have_stray_prices_masked(self):
        answer, _, _ = self.ask(answer_json("外径 47mm [1]，参考价 ¥9999", citations=[1]), role="客服")
        self.assertNotIn("9999", answer.text)
        self.assertIn("***", answer.text)

    def test_invalid_json_is_retried_once(self):
        answer, transport, _ = self.ask("not json", answer_json("外径 47mm [1]", citations=[1]))
        self.assertEqual((answer.status, transport.count("chat")), ("answered", 2))
        self.assertEqual([s.name for s in answer.trace.stages][-2:], ["generate", "generate_retry"])
        answer, _, _ = self.ask("not json", "still not json")
        self.assertEqual(answer.status, "error")

    def test_substitute_question_adds_candidates_and_their_rows(self):
        answer, transport, _ = self.ask(answer_json("可用 6205-ZZ 替代 [1]", citations=[1]),
                                        question="6205-2RS 缺货，有什么可以替代？", role="客服")
        self.assertEqual([a.product.spec for a in answer.alternatives], ["6205-ZZ"])
        prompt = transport.calls[-1][1]["messages"][1]["content"]
        self.assertIn("替代候选：6205-ZZ（物料编码 1.01.0012）", prompt)
        self.assertIn("6 选型与替代说明", prompt)

    def test_filters_restrict_context_and_empty_filters_refuse(self):
        answer, transport, _ = self.ask(answer_json("FKM O型圈有 8 种 [1]", citations=[1]),
                                        question="有哪些 O型圈", role="客服", filters=Filters(material="FKM"))
        self.assertTrue(answer.hits and all(h.chunk.row and "FKM" in h.chunk.row["材质"] for h in answer.hits))
        self.assertIn("筛选条件：材质含“FKM”", transport.calls[-1][1]["messages"][1]["content"])
        answer, transport, _ = self.ask(question="有哪些 O型圈", filters=Filters(material="硅胶"))
        self.assertEqual((answer.status, transport.calls), ("refused", []))

    def test_quote_draft(self):
        parsed = json.dumps({"customer": "沪东衡砺", "items": [{"product": "6204-2RS", "quantity": 200, "unit": "个"},
                                                              {"product": "DN25 316", "quantity": 2, "unit": "个"}]})
        answer, _, a = self.ask(parsed, question="给沪东衡砺报个价：6204-2RS 200个，DN25 316 球阀 2个")
        self.assertEqual(answer.status, "quote")
        self.assertEqual(answer.quote.total, round(200 * 10.8 + 2 * 126.4, 2))
        self.assertTrue(answer.quote.path and answer.quote.path.exists())
        self.assertEqual([c.locator for c in answer.citations], ["价目表!r289"])   # list-price line cites the row
        self.assertEqual(answer.to_dict()["quote"]["lines"][0]["price_basis"], "协议价")

    def test_query_log_stores_a_hash_not_the_user_id(self):
        answer, _, a = self.ask(answer_json("外径 47mm [1]", citations=[1]))
        with closing(sqlite3.connect(a.settings.query_log)) as db:
            row = db.execute("SELECT user_hash, role, status, citations FROM queries WHERE id=?",
                             (answer.log_id,)).fetchone()
        self.assertEqual(row[1:3], ("销售", "answered"))
        self.assertNotIn("ou_secret_user", json.dumps(row))
        self.assertEqual(len(row[0]), 12)
        self.assertEqual(a.log.recent(1)[0]["question"], "6204-2RS 的外径是多少？")


if __name__ == "__main__":
    unittest.main()
