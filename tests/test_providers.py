"""Record / replay, the embedding cache, response parsing and cost estimation — all without network."""
from __future__ import annotations

import datetime as dt
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from catalog_rag.providers import (ChatClient, EmbeddingCache, EmbeddingClient, LiveTransport, ProviderError,
                                   RecordingTransport, ReplayTransport, RerankClient, request_key)
from catalog_rag.tracing import Stage, Trace, cost_usd, deepseek_peak
from tests.support import FakeTransport, chat_payload


class Inner:
    """Live stand-in that answers each call with a numbered payload."""

    def __init__(self):
        self.n = 0

    def post(self, url, body, *, secret):  # noqa: ANN001 - mirrors Transport
        self.n += 1
        return chat_payload(f"answer {self.n}"), 100.0 + self.n, False


class TempDirTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="providers_"))
        self.addCleanup(shutil.rmtree, self.dir, True)


class RecordReplayTest(TempDirTest):
    def test_replay_returns_recorded_payload_and_latency(self):
        recorder = RecordingTransport(Inner(), self.dir, run="bm25")
        recorder.post("https://x/chat/completions", {"model": "m", "q": 1}, secret="chat")
        record = json.loads(next(self.dir.glob("*.json")).read_text(encoding="utf-8"))
        self.assertEqual((record["source"], record["model"], record["calls"][0]["run"]), ("live", "m", "bm25"))
        self.assertNotIn("Authorization", json.dumps(record))
        payload, ms, replayed = ReplayTransport(self.dir, "bm25").post("https://x/chat/completions",
                                                                       {"model": "m", "q": 1}, secret="chat")
        self.assertEqual((payload["choices"][0]["message"]["content"], ms, replayed), ("answer 1", 101.0, True))

    def test_identical_requests_from_two_runs_replay_separately(self):
        inner = Inner()
        for run in ("bm25", "hybrid"):
            RecordingTransport(inner, self.dir, run=run).post("https://x/c", {"q": 1}, secret="chat")
        for run, expected in (("hybrid", "answer 2"), ("bm25", "answer 1")):
            payload, _, _ = ReplayTransport(self.dir, run).post("https://x/c", {"q": 1}, secret="chat")
            self.assertEqual(payload["choices"][0]["message"]["content"], expected)

    def test_replay_never_invents(self):
        with self.assertRaises(ProviderError):
            ReplayTransport(self.dir).post("https://x/c", {"q": 2}, secret="chat")
        handmade = {"source": "handmade", "calls": [{"response": chat_payload("made up")}]}
        (self.dir / f"{request_key('https://x/c', {'q': 2})}.json").write_text(json.dumps(handmade), encoding="utf-8")
        with self.assertRaises(ProviderError):
            ReplayTransport(self.dir).post("https://x/c", {"q": 2}, secret="chat")

    def test_live_transport_needs_a_key(self):
        with self.assertRaises(ProviderError):
            LiveTransport({"chat": None}).post("https://example.invalid", {}, secret="chat")


class EmbeddingTest(TempDirTest):
    def test_cache_round_trip_and_batching(self):
        transport = FakeTransport()
        client = EmbeddingClient(transport, EmbeddingCache(self.dir / "c.jsonl"), "fake", 64)
        texts = [f"text {i}" for i in range(23)]
        vectors, stage = client.embed(texts)
        self.assertEqual((len(vectors), transport.count("embeddings"), stage.input_tokens), (23, 3, 161))
        reloaded = EmbeddingClient(None, EmbeddingCache(self.dir / "c.jsonl"), "fake", 64)
        again, stage = reloaded.embed(texts[:2])
        self.assertAlmostEqual(again[1][0], vectors[1][0], places=6)
        self.assertTrue(stage.replayed)
        with self.assertRaises(ProviderError):
            reloaded.embed(["never seen"])


class ParsingAndCostTest(unittest.TestCase):
    def test_chat_usage_and_cache_hits(self):
        transport = FakeTransport("{}")
        content, stage = ChatClient(transport, "https://api.deepseek.com", "deepseek-flash", "deepseek") \
            .complete([{"role": "user", "content": "hi"}])
        self.assertEqual((content, stage.input_tokens, stage.output_tokens), ("{}", 100, 20))
        self.assertEqual(transport.calls[0][1]["response_format"], {"type": "json_object"})
        self.assertEqual(transport.calls[0][1]["temperature"], 0)

    def test_rerank_parsing(self):
        transport = FakeTransport(rerank=lambda q, docs: [0.1, 0.9, 0.5])
        results, stage = RerankClient(transport, "gte-rerank-v2").rerank("q", ["a", "b", "c"], top_n=2)
        self.assertEqual(results, [(1, 0.9), (2, 0.5)])
        self.assertEqual(stage.input_tokens, 300)

    def test_cost_estimates(self):
        off_peak = dt.datetime(2026, 11, 18, 12, tzinfo=dt.timezone.utc)
        peak = dt.datetime(2026, 11, 18, 8, tzinfo=dt.timezone.utc)
        self.assertFalse(deepseek_peak(off_peak))
        self.assertTrue(deepseek_peak(peak))
        base = cost_usd("deepseek-flash", 1_000_000, 0, 0, off_peak)
        self.assertAlmostEqual(base, 0.15)
        self.assertAlmostEqual(cost_usd("deepseek-flash", 1_000_000, 0, 0, peak), 0.30)
        self.assertAlmostEqual(cost_usd("deepseek-flash", 1_000_000, 0, 1_000_000, off_peak), 0.003)
        self.assertEqual(cost_usd("unknown-model", 10, 10), 0.0)

    def test_trace_totals(self):
        trace = Trace()
        trace.add(Stage("a", 10.0, "m", "deepseek", 5, 2, cost_usd=0.001))
        trace.add(Stage("b", 5.5))
        self.assertEqual(trace.total_ms, 15.5)
        self.assertEqual(trace.tokens_by_provider(), {"deepseek": {"input": 5, "output": 2, "cached": 0, "calls": 1}})
        self.assertIn("耗时", trace.summary())


if __name__ == "__main__":
    unittest.main()
