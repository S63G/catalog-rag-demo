"""Retrieval pipelines: BM25 only, hybrid (BM25 + vectors fused with RRF), hybrid + Bailian rerank.

Role permissions and metadata filters are applied *before* ranking, as an allow-list of chunk ids, so a chunk the
asker may not see can never reach the model.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np

from .config import Pipeline
from .documents import Chunk
from .index import Index
from .providers import EmbeddingClient, ProviderError, RerankClient
from .text import tokenize
from .tracing import Stage, Trace

CANDIDATES = 30      # per retriever, before fusion
RERANK_POOL = 20     # fused candidates sent to the reranker
RRF_K = 60


@dataclass
class Hit:
    chunk: Chunk
    score: float
    ranks: dict[str, int] = field(default_factory=dict)   # 1-based rank per stage, e.g. {"bm25": 2, "vector": 5}


class Retriever:
    def __init__(self, index: Index, pipeline: Pipeline, embedder: EmbeddingClient | None,
                 reranker: RerankClient | None):
        if pipeline != "bm25" and (index.vectors is None or embedder is None):
            raise RuntimeError("向量索引不完整，请先运行 python -m catalog_rag ingest")
        if pipeline == "hybrid_rerank" and reranker is None:
            raise RuntimeError("hybrid_rerank 需要 rerank 客户端")
        self.index, self.pipeline, self.embedder, self.reranker = index, pipeline, embedder, reranker

    def search(self, query: str, allowed: set[str], k: int, trace: Trace) -> tuple[list[Hit], list[str]]:
        """Top-``k`` hits among ``allowed`` chunk ids, plus warnings (e.g. degraded to BM25 after a provider error)."""
        mask = np.array([c.id in allowed for c in self.index.chunks])
        if not mask.any():
            return [], []
        bm25 = self._bm25(query, mask, trace)
        bm25_hits = [Hit(self.index.chunks[i], s, {"bm25": r}) for r, (i, s) in enumerate(bm25[:k], 1)]
        if self.pipeline == "bm25":
            return bm25_hits, []
        try:
            vector = self._vector(query, mask, trace)
        except ProviderError as exc:
            return bm25_hits, [f"向量检索不可用（{_short(exc)}），本次已降级为 BM25"]
        fused = _rrf({"bm25": bm25, "vector": vector})
        hits = [Hit(self.index.chunks[i], s, ranks) for i, s, ranks in fused]
        if self.pipeline == "hybrid":
            return hits[:k], []
        assert self.reranker is not None
        pool = hits[:RERANK_POOL]
        try:
            results, stage = self.reranker.rerank(query, [h.chunk.text for h in pool], top_n=min(k, len(pool)))
        except ProviderError as exc:
            return hits[:k], [f"重排服务不可用（{_short(exc)}），本次使用未重排的混合检索结果"]
        trace.add(stage)
        out = []
        for rank, (i, score) in enumerate(results, 1):
            hit = pool[i]
            out.append(Hit(hit.chunk, score, {**hit.ranks, "rerank": rank}))
        return out, []

    def _bm25(self, query: str, mask: np.ndarray, trace: Trace) -> list[tuple[int, float]]:
        start = time.perf_counter()
        scores = np.asarray(self.index.bm25.get_scores(tokenize(query)), dtype=np.float64)
        scores[~mask] = -np.inf
        order = [int(i) for i in np.argsort(-scores)[:CANDIDATES] if np.isfinite(scores[i]) and scores[i] > 0]
        trace.add(Stage("bm25", round((time.perf_counter() - start) * 1000, 1)))
        return [(i, float(scores[i])) for i in order]

    def _vector(self, query: str, mask: np.ndarray, trace: Trace) -> list[tuple[int, float]]:
        assert self.embedder is not None and self.index.vectors is not None
        [vector], stage = self.embedder.embed([query], stage_name="embed_query")
        trace.add(stage)
        start = time.perf_counter()
        q = np.asarray(vector, dtype=np.float32)
        sims = self.index.vectors @ (q / np.linalg.norm(q))
        sims[~mask] = -np.inf
        order = [int(i) for i in np.argsort(-sims)[:CANDIDATES] if np.isfinite(sims[i])]
        trace.add(Stage("vector", round((time.perf_counter() - start) * 1000, 1)))
        return [(i, float(sims[i])) for i in order]


def _short(exc: Exception) -> str:
    return str(exc).split(":", 1)[0][:60]


def _rrf(rankings: dict[str, list[tuple[int, float]]]) -> list[tuple[int, float, dict[str, int]]]:
    """Reciprocal rank fusion: score = sum over retrievers of 1 / (60 + rank)."""
    scores: dict[int, float] = {}
    ranks: dict[int, dict[str, int]] = {}
    for name, ranked in rankings.items():
        for rank, (i, _) in enumerate(ranked, 1):
            scores[i] = scores.get(i, 0.0) + 1.0 / (RRF_K + rank)
            ranks.setdefault(i, {})[name] = rank
    order = sorted(scores, key=lambda i: -scores[i])
    return [(i, scores[i], ranks[i]) for i in order]
