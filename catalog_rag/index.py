"""Ingestion (documents -> chunks, catalogue, embeddings) and the loaded search index."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from rank_bm25 import BM25Okapi

from .catalog import Catalog, load_customers
from .config import Settings
from .documents import Chunk, parse_folder
from .providers import EmbeddingCache, EmbeddingClient
from .text import tokenize
from .tracing import Stage

CHUNKS_FILE = "chunks.jsonl"
CATALOG_FILE = "catalog.json"


@dataclass
class IngestReport:
    documents: int
    chunks: dict[str, int]
    products: int
    embedding: Stage
    cached_vectors: int


def ingest(settings: Settings, embedder: EmbeddingClient) -> IngestReport:
    chunks = parse_folder(settings.docs_dir)
    settings.index_dir.mkdir(parents=True, exist_ok=True)
    with (settings.index_dir / CHUNKS_FILE).open("w", encoding="utf-8") as fh:
        for c in chunks:
            fh.write(json.dumps(c.to_dict(), ensure_ascii=False) + "\n")
    catalog = Catalog.from_chunks(chunks, load_customers(settings.master_dir / "customers.json"))
    catalog.save(settings.index_dir / CATALOG_FILE)
    _, stage = embedder.embed([c.text for c in chunks], stage_name="embed_chunks")
    kinds: dict[str, int] = {}
    for c in chunks:
        kinds[c.kind] = kinds.get(c.kind, 0) + 1
    return IngestReport(len({c.doc for c in chunks}), kinds, len(catalog.products), stage, len(embedder.cache))


class Index:
    def __init__(self, chunks: list[Chunk], catalog: Catalog, vectors: np.ndarray | None):
        self.chunks = chunks
        self.by_id = {c.id: c for c in chunks}
        self.catalog = catalog
        self.vectors = vectors                     # L2-normalised, one row per chunk
        self.bm25 = BM25Okapi([tokenize(c.text) for c in chunks])

    @classmethod
    def load(cls, settings: Settings) -> "Index":
        path = settings.index_dir / CHUNKS_FILE
        if not path.exists():
            raise FileNotFoundError("索引不存在，请先运行 python -m catalog_rag ingest")
        chunks = [Chunk(**json.loads(line)) for line in path.read_text(encoding="utf-8").splitlines() if line]
        catalog = Catalog.load(settings.index_dir / CATALOG_FILE, settings.master_dir / "customers.json")
        cache = EmbeddingCache(settings.embedding_cache)
        rows = [cache.get(EmbeddingCache.key(settings.embed_model, settings.embed_dim, c.text)) for c in chunks]
        vectors = None
        if all(r is not None for r in rows):
            matrix = np.asarray(rows, dtype=np.float32)
            vectors = matrix / np.linalg.norm(matrix, axis=1, keepdims=True)
        return cls(chunks, catalog, vectors)

    def section_chunk(self, doc: str, keyword: str) -> Chunk | None:
        return next((c for c in self.chunks if c.doc == doc and c.kind == "text" and keyword in c.title), None)


def docs_listing(index: Index) -> list[str]:
    return sorted({c.doc for c in index.chunks})


def doc_path(settings: Settings, name: str) -> Path | None:
    """Resolve a document name to a file inside the docs folder (no path traversal)."""
    candidate = (settings.docs_dir / name).resolve()
    if candidate.parent != settings.docs_dir.resolve() or not candidate.is_file():
        return None
    return candidate
