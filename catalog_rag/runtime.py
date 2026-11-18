"""Wire settings to clients: live (optionally recording) or replay."""
from __future__ import annotations

from .assistant import Assistant
from .config import Settings
from .index import Index
from .providers import (ChatClient, EmbeddingCache, EmbeddingClient, LiveTransport, RecordingTransport,
                        ReplayTransport, RerankClient, Transport)
from .querylog import QueryLog
from .retrieval import Retriever


def _transports(settings: Settings) -> tuple[Transport | None, Transport]:
    """(transport for embeddings or None = cache only, transport for rerank + chat)."""
    run = f"{settings.pipeline}/{settings.chat_model}"   # e.g. "hybrid_rerank/qwen-plus"
    if settings.mode == "replay":
        return None, ReplayTransport(settings.replay_dir, run)
    live = LiveTransport({"dashscope": settings.dashscope_api_key, "chat": settings.chat_api_key})
    return live, (RecordingTransport(live, settings.replay_dir, run) if settings.record else live)


def build_embedder(settings: Settings) -> EmbeddingClient:
    embed_transport, _ = _transports(settings)
    return EmbeddingClient(embed_transport, EmbeddingCache(settings.embedding_cache), settings.embed_model,
                           settings.embed_dim)


def build_assistant(settings: Settings, index: Index | None = None) -> Assistant:
    index = index or Index.load(settings)
    embedder = build_embedder(settings)
    _, transport = _transports(settings)
    reranker = RerankClient(transport, settings.rerank_model)
    retriever = Retriever(index, settings.pipeline, embedder, reranker)
    chat = ChatClient(transport, settings.chat_base_url, settings.chat_model, settings.chat_provider)
    return Assistant(settings, index, retriever, chat, QueryLog(settings.query_log))
