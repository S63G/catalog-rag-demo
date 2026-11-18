"""Which keys each mode and retrieval pipeline needs."""
from __future__ import annotations

import os
import unittest
from unittest import mock

from catalog_rag.config import Settings

CLEAN = {k: "" for k in ("LLM_API_KEY", "DASHSCOPE_API_KEY", "CATALOG_MODE", "CATALOG_PIPELINE", "LLM_PROVIDER")}


def settings(**env: str) -> Settings:
    with mock.patch.dict(os.environ, {**CLEAN, **env}):
        return Settings.from_env()


class ConfigTest(unittest.TestCase):
    def test_no_keys_means_replay(self) -> None:
        self.assertEqual(settings().mode, "replay")

    def test_bm25_with_deepseek_runs_live_without_bailian(self) -> None:
        s = settings(LLM_API_KEY="test", CATALOG_PIPELINE="bm25")
        self.assertEqual((s.mode, s.pipeline), ("live", "bm25"))

    def test_vector_pipelines_need_bailian_to_go_live(self) -> None:
        self.assertEqual(settings(LLM_API_KEY="test").mode, "replay")
        with self.assertRaisesRegex(ValueError, "DASHSCOPE_API_KEY"):
            settings(LLM_API_KEY="test", CATALOG_MODE="live", CATALOG_PIPELINE="hybrid")

    def test_qwen_answers_need_bailian_even_with_bm25(self) -> None:
        with self.assertRaisesRegex(ValueError, "DASHSCOPE_API_KEY"):
            settings(LLM_PROVIDER="qwen", CATALOG_MODE="live", CATALOG_PIPELINE="bm25")

    def test_both_keys_default_to_live(self) -> None:
        self.assertEqual(settings(LLM_API_KEY="a", DASHSCOPE_API_KEY="b").mode, "live")


if __name__ == "__main__":
    unittest.main()
