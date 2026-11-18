"""Shared test helpers: the committed catalogue parsed once, hashed fake embeddings, a fake HTTP transport."""
from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from pathlib import Path
from typing import Callable

import numpy as np

from catalog_rag.assistant import Assistant
from catalog_rag.catalog import Catalog, load_customers
from catalog_rag.config import PROJECT_ROOT, Pipeline, Settings
from catalog_rag.documents import Chunk, parse_folder
from catalog_rag.index import Index
from catalog_rag.providers import ChatClient, EmbeddingCache, EmbeddingClient, ProviderError, RerankClient
from catalog_rag.querylog import QueryLog
from catalog_rag.retrieval import Retriever
from catalog_rag.text import tokenize

DATA = PROJECT_ROOT / "data"
DIM = 64
_CHUNKS: list[Chunk] | None = None
_INDEX: Index | None = None


def chunks() -> list[Chunk]:
    global _CHUNKS
    if _CHUNKS is None:
        _CHUNKS = parse_folder(DATA / "docs")
    return _CHUNKS


def truth() -> dict:
    return json.loads((DATA / "catalog_truth.json").read_text(encoding="utf-8"))


def catalog() -> Catalog:
    return Catalog.from_chunks(chunks(), load_customers(DATA / "master" / "customers.json"))


def hash_vector(text: str) -> list[float]:
    """Deterministic bag-of-tokens embedding: good enough to exercise the vector path without a model."""
    v = np.zeros(DIM, dtype=np.float32)
    for token in tokenize(text):
        v[int(hashlib.md5(token.encode()).hexdigest(), 16) % DIM] += 1.0
    return (v / (np.linalg.norm(v) or 1.0)).tolist()


def index() -> Index:
    global _INDEX
    if _INDEX is None:
        cs = chunks()
        _INDEX = Index(cs, catalog(), np.asarray([hash_vector(c.text) for c in cs], dtype=np.float32))
    return _INDEX


def chat_payload(content: str, prompt: int = 100, completion: int = 20, cached: int = 0) -> dict:
    return {"choices": [{"message": {"content": content}}], "created": 1763452800,
            "usage": {"prompt_tokens": prompt, "completion_tokens": completion, "prompt_cache_hit_tokens": cached}}


def answer_json(answer: str, answerable: bool = True, citations: list[int] | None = None) -> str:
    return json.dumps({"answerable": answerable, "answer": answer, "citations": citations or []}, ensure_ascii=False)


class FakeTransport:
    """Stands in for the HTTP layer: hashed embeddings, a pluggable reranker, queued chat responses."""

    def __init__(self, *chat: str, rerank: Callable[[str, list[str]], list[float]] | None = None,
                 fail: set[str] | None = None):
        self.chat = list(chat)
        self.rerank_fn = rerank or (lambda q, docs: [float(len(set(tokenize(q)) & set(tokenize(d)))) for d in docs])
        self.fail = fail or set()
        self.calls: list[tuple[str, dict]] = []

    def post(self, url: str, body: dict, *, secret: str) -> tuple[dict, float, bool]:
        kind = "embeddings" if url.endswith("/embeddings") else "rerank" if "rerank" in url else "chat"
        self.calls.append((kind, body))
        if kind in self.fail:
            raise ProviderError(f"HTTP 400 from fake: {kind} unavailable")
        if kind == "embeddings":
            data = [{"index": i, "embedding": hash_vector(t)} for i, t in enumerate(body["input"])]
            return {"data": data, "usage": {"total_tokens": 7 * len(body["input"])}}, 5.0, False
        if kind == "rerank":
            scores = self.rerank_fn(body["input"]["query"], body["input"]["documents"])
            order = sorted(range(len(scores)), key=lambda i: -scores[i])[:body["parameters"]["top_n"]]
            return {"output": {"results": [{"index": i, "relevance_score": scores[i]} for i in order]},
                    "usage": {"total_tokens": 300}}, 8.0, False
        if not self.chat:
            raise AssertionError("FakeTransport ran out of chat responses")
        return chat_payload(self.chat.pop(0)), 12.0, False

    def count(self, kind: str) -> int:
        return sum(1 for k, _ in self.calls if k == kind)


def settings(out_dir: Path, pipeline: Pipeline = "hybrid_rerank") -> Settings:
    return Settings(data_dir=DATA, out_dir=out_dir, mode="live", pipeline=pipeline, embed_model="fake-embed",
                    embed_dim=DIM)


def assistant(add_cleanup: Callable[..., None], transport: FakeTransport,
              pipeline: Pipeline = "hybrid_rerank") -> Assistant:
    out = Path(tempfile.mkdtemp(prefix="catalog_rag_test_"))
    add_cleanup(shutil.rmtree, out, True)
    s = settings(out, pipeline)
    embedder = EmbeddingClient(transport, EmbeddingCache(out / "cache.jsonl"), s.embed_model, DIM)
    retriever = Retriever(index(), pipeline, embedder, RerankClient(transport, s.rerank_model))
    chat = ChatClient(transport, "https://api.deepseek.com", "deepseek-flash", "deepseek")
    return Assistant(s, index(), retriever, chat, QueryLog(s.query_log))
