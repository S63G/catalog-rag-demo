"""HTTP clients for Bailian (embeddings, rerank, qwen chat) and DeepSeek (chat), with record / replay.

Only the standard library is used for HTTP. API keys live in :class:`LiveTransport` and are sent as headers; they
are never part of a recorded request. Recordings store the provider's raw JSON response (including ``usage``) and
the measured latency, tagged ``source: live``. Replay serves exactly those responses and refuses to invent one.

Embeddings are cached per text in a JSONL file; that cache doubles as the replay record for vectors.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import time
import urllib.error
import urllib.request
from array import array
from pathlib import Path
from typing import Protocol

from .config import DASHSCOPE_COMPAT, DASHSCOPE_RERANK
from .tracing import Stage, cost_usd


class ProviderError(RuntimeError):
    """A provider call failed, or replay has no recording for the request."""


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def request_key(url: str, body: dict) -> str:
    blob = json.dumps({"url": url, "body": body}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class Transport(Protocol):
    def post(self, url: str, body: dict, *, secret: str) -> tuple[dict, float, bool]:
        """Return (response JSON, latency in ms, replayed?). ``secret`` names the key: "dashscope" or "chat"."""
        ...


class LiveTransport:
    def __init__(self, secrets: dict[str, str | None], timeout: float = 60.0):
        self._secrets = secrets
        self.timeout = timeout

    def post(self, url: str, body: dict, *, secret: str) -> tuple[dict, float, bool]:
        key = self._secrets.get(secret)
        if not key:
            raise ProviderError(f"no API key configured for {secret}")
        request = urllib.request.Request(
            url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
        start = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            with exc:
                detail = exc.read().decode("utf-8", "replace")[:300]
            raise ProviderError(f"HTTP {exc.code} from {url.split('/')[2]}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise ProviderError(f"cannot reach {url.split('/')[2]}: {exc}") from exc
        return payload, round((time.perf_counter() - start) * 1000, 1), False


class RecordingTransport:
    """Live calls, each appended to ``<folder>/<request sha256>.json`` for later replay.

    ``run`` tags each call ("<pipeline>/<answer model>"), because two runs can send a byte-identical request — BM25
    and hybrid retrieving the same five chunks, or the same rerank call made in a DeepSeek and a qwen run — and replay
    must give each run its own recorded response.
    """

    def __init__(self, inner: Transport, folder: Path, run: str = ""):
        self.inner, self.folder, self.run = inner, folder, run

    def post(self, url: str, body: dict, *, secret: str) -> tuple[dict, float, bool]:
        payload, ms, _ = self.inner.post(url, body, secret=secret)
        self.folder.mkdir(parents=True, exist_ok=True)
        key = request_key(url, body)
        path = self.folder / f"{key}.json"
        record = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {
            "source": "live", "endpoint": url, "model": body.get("model"), "request_sha256": key,
            "request": body, "calls": []}
        record["calls"].append({"recorded_at": _now().isoformat(timespec="seconds"), "run": self.run,
                                "latency_ms": ms, "response": payload})
        path.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
        return payload, ms, False


class ReplayTransport:
    """Serves recorded live calls in recorded order (preferring calls tagged with the same run); never invents one."""

    def __init__(self, folder: Path, run: str = ""):
        self.folder, self.run = folder, run
        self._served: dict[str, int] = {}

    def post(self, url: str, body: dict, *, secret: str) -> tuple[dict, float, bool]:
        key = request_key(url, body)
        path = self.folder / f"{key}.json"
        if not path.exists():
            raise ProviderError("回放模式下没有这个请求的真实录制结果（需要 live 模式）")
        record = json.loads(path.read_text(encoding="utf-8"))
        if record.get("source") != "live" or not record.get("calls"):
            raise ProviderError(f"replay record {path.name} was not produced by a live run")
        calls = [c for c in record["calls"] if c.get("run") == self.run] or record["calls"]
        index = self._served.get(key, 0)
        self._served[key] = index + 1
        call = calls[min(index, len(calls) - 1)]
        return call["response"], float(call.get("latency_ms", 0.0)), True


# --------------------------------------------------------------------------- embeddings


class EmbeddingCache:
    """Append-only JSONL: one line per (model, dim, text) with the vector as base64 float32."""

    def __init__(self, path: Path):
        self.path = path
        self._vectors: dict[str, list[float]] = {}
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    row = json.loads(line)
                    self._vectors[row["key"]] = list(array("f", base64.b64decode(row["vector"])))

    @staticmethod
    def key(model: str, dim: int, text: str) -> str:
        return hashlib.sha256(f"{model}|{dim}|{text}".encode("utf-8")).hexdigest()

    def get(self, key: str) -> list[float] | None:
        return self._vectors.get(key)

    def __getitem__(self, key: str) -> list[float]:
        return self._vectors[key]

    def put_many(self, rows: list[tuple[str, list[float]]], model: str, dim: int) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stamp = _now().isoformat(timespec="seconds")
        with self.path.open("a", encoding="utf-8") as fh:
            for key, vector in rows:
                self._vectors[key] = vector
                blob = base64.b64encode(array("f", vector).tobytes()).decode("ascii")
                fh.write(json.dumps({"key": key, "model": model, "dim": dim, "source": "live",
                                     "recorded_at": stamp, "vector": blob}) + "\n")

    def __len__(self) -> int:
        return len(self._vectors)


class EmbeddingClient:
    """Bailian OpenAI-compatible embeddings; ``transport=None`` means cache only (replay)."""

    batch_size = 10

    def __init__(self, transport: Transport | None, cache: EmbeddingCache, model: str, dim: int):
        self.transport, self.cache, self.model, self.dim = transport, cache, model, dim

    def embed(self, texts: list[str], stage_name: str = "embed") -> tuple[list[list[float]], Stage]:
        keys = [EmbeddingCache.key(self.model, self.dim, t) for t in texts]
        missing = [(k, t) for k, t in dict(zip(keys, texts)).items() if self.cache.get(k) is None]
        stage = Stage(stage_name, 0.0, self.model, "bailian")
        if missing:
            if self.transport is None:
                raise ProviderError("向量缓存里没有这段文本，回放模式无法计算新的向量（需要 live 模式）")
            for i in range(0, len(missing), self.batch_size):
                batch = missing[i:i + self.batch_size]
                body = {"model": self.model, "input": [t for _, t in batch], "dimensions": self.dim,
                        "encoding_format": "float"}
                payload, ms, _ = self.transport.post(f"{DASHSCOPE_COMPAT}/embeddings", body, secret="dashscope")
                data = sorted(payload["data"], key=lambda d: d["index"])
                self.cache.put_many([(k, d["embedding"]) for (k, _), d in zip(batch, data)], self.model, self.dim)
                stage.ms += ms
                stage.input_tokens += int((payload.get("usage") or {}).get("total_tokens", 0))
            stage.cost_usd = cost_usd(self.model, stage.input_tokens, 0)
        else:
            stage.replayed = self.transport is None  # replay mode: vectors recorded during a live run
        return [self.cache[k] for k in keys], stage


# --------------------------------------------------------------------------- rerank


class RerankClient:
    def __init__(self, transport: Transport, model: str):
        self.transport, self.model = transport, model

    def rerank(self, query: str, documents: list[str], top_n: int) -> tuple[list[tuple[int, float]], Stage]:
        body = {"model": self.model, "input": {"query": query, "documents": documents},
                "parameters": {"top_n": top_n, "return_documents": False}}
        payload, ms, replayed = self.transport.post(DASHSCOPE_RERANK, body, secret="dashscope")
        try:
            results = [(int(r["index"]), float(r["relevance_score"])) for r in payload["output"]["results"]]
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderError(f"unexpected rerank response: {str(payload)[:200]}") from exc
        tokens = int((payload.get("usage") or {}).get("total_tokens", 0))
        stage = Stage("rerank", ms, self.model, "bailian", input_tokens=tokens,
                      cost_usd=cost_usd(self.model, tokens, 0), replayed=replayed)
        return results, stage


# --------------------------------------------------------------------------- chat


Message = dict[str, str]


class ChatClient:
    """OpenAI-compatible chat completions (DeepSeek or qwen via Bailian compatible mode)."""

    def __init__(self, transport: Transport, base_url: str, model: str, provider: str):
        self.transport, self.base_url, self.model, self.provider = transport, base_url.rstrip("/"), model, provider

    def complete(self, messages: list[Message], stage_name: str = "generate") -> tuple[str, Stage]:
        body = {"model": self.model, "messages": messages, "temperature": 0,
                "response_format": {"type": "json_object"}}
        payload, ms, replayed = self.transport.post(f"{self.base_url}/chat/completions", body, secret="chat")
        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f"unexpected chat response: {str(payload)[:200]}") from exc
        usage = payload.get("usage") or {}
        cached = int(usage.get("prompt_cache_hit_tokens")
                     or (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
        prompt, completion = int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0))
        when = dt.datetime.fromtimestamp(payload["created"], dt.timezone.utc) if payload.get("created") else _now()
        stage = Stage(stage_name, ms, self.model, self.provider, prompt, completion, cached,
                      cost_usd(self.model, prompt, completion, cached, when), replayed)
        return content, stage
