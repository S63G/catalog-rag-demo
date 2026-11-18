"""Runtime settings, read from environment variables. Keys are only ever read here, never logged or written."""
from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DISTRIBUTOR = "瑞丰机电批发（虚构）"

Mode = Literal["live", "replay"]
Pipeline = Literal["bm25", "hybrid", "hybrid_rerank"]
PIPELINES: tuple[Pipeline, ...] = ("bm25", "hybrid", "hybrid_rerank")
ROLES = ("销售", "客服")

DASHSCOPE_COMPAT = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DASHSCOPE_RERANK = "https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank"

# OpenAI-compatible chat endpoints of the two answer-generation providers.
CHAT_PROVIDERS: dict[str, dict[str, str]] = {
    "deepseek": {"base_url": "https://api.deepseek.com", "model": "deepseek-flash", "key_env": "LLM_API_KEY"},
    "qwen": {"base_url": DASHSCOPE_COMPAT, "model": "qwen-plus", "key_env": "DASHSCOPE_API_KEY"},
}


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    out_dir: Path
    mode: Mode = "replay"
    record: bool = False
    pipeline: Pipeline = "hybrid_rerank"
    chat_provider: str = "deepseek"
    chat_model: str = "deepseek-flash"
    chat_base_url: str = CHAT_PROVIDERS["deepseek"]["base_url"]
    chat_api_key: str | None = None
    dashscope_api_key: str | None = None
    embed_model: str = "text-embedding-v4"
    embed_dim: int = 512
    rerank_model: str = "gte-rerank-v2"
    price_tolerance: float = 0.02
    feishu_app_id: str | None = None
    feishu_app_secret: str | None = None
    feishu_default_role: str = "客服"
    feishu_user_roles: str = ""
    feishu_allow_role_tag: bool = False

    @classmethod
    def from_env(cls, data_dir: Path | None = None, out_dir: Path | None = None) -> "Settings":
        env = os.environ
        provider = (env.get("LLM_PROVIDER") or "deepseek").lower()  # empty counts as unset (.env.example copies)
        if provider not in CHAT_PROVIDERS:
            raise ValueError(f"LLM_PROVIDER must be one of {sorted(CHAT_PROVIDERS)}, not {provider!r}")
        spec = CHAT_PROVIDERS[provider]
        chat_key = env.get(spec["key_env"]) or None
        dashscope_key = env.get("DASHSCOPE_API_KEY") or None
        pipeline = env.get("CATALOG_PIPELINE") or "hybrid_rerank"
        if pipeline not in PIPELINES:
            raise ValueError(f"CATALOG_PIPELINE must be one of {PIPELINES}, not {pipeline!r}")
        # BM25 + DeepSeek runs live without Bailian; vectors, rerank or qwen answers need its key.
        needs_dashscope = pipeline != "bm25" or provider == "qwen"
        keys_ready = bool(chat_key) and (bool(dashscope_key) or not needs_dashscope)
        mode = (env.get("CATALOG_MODE") or ("live" if keys_ready else "replay")).lower()
        if mode not in ("live", "replay"):
            raise ValueError(f"CATALOG_MODE must be live or replay, not {mode!r}")
        if mode == "live" and not keys_ready:
            needed = "DASHSCOPE_API_KEY and " + spec["key_env"] if needs_dashscope else spec["key_env"]
            raise ValueError(f"CATALOG_MODE=live with CATALOG_PIPELINE={pipeline} needs {needed}")
        default_role = env.get("FEISHU_DEFAULT_ROLE", "客服")
        if default_role not in ROLES:
            raise ValueError(f"FEISHU_DEFAULT_ROLE must be one of {ROLES}")
        return cls(
            data_dir=data_dir or Path(env.get("CATALOG_DATA", PROJECT_ROOT / "data")),
            out_dir=out_dir or Path(env.get("CATALOG_OUT", PROJECT_ROOT / "out")),
            mode=mode,  # type: ignore[arg-type]
            record=env.get("CATALOG_RECORD", "0") == "1",
            pipeline=pipeline,  # type: ignore[arg-type]
            chat_provider=provider,
            chat_model=env.get("LLM_MODEL") or spec["model"],
            chat_base_url=spec["base_url"],
            chat_api_key=chat_key,
            dashscope_api_key=dashscope_key,
            embed_model=env.get("EMBED_MODEL", "text-embedding-v4"),
            embed_dim=int(env.get("EMBED_DIM", "512")),
            rerank_model=env.get("RERANK_MODEL", "gte-rerank-v2"),
            price_tolerance=float(env.get("PRICE_TOLERANCE", "0.02")),
            feishu_app_id=env.get("FEISHU_APP_ID") or None,
            feishu_app_secret=env.get("FEISHU_APP_SECRET") or None,
            feishu_default_role=default_role,
            feishu_user_roles=env.get("FEISHU_USER_ROLES", ""),
            feishu_allow_role_tag=env.get("FEISHU_ALLOW_ROLE_TAG", "0") == "1",
        )

    def with_pipeline(self, pipeline: Pipeline) -> "Settings":
        return replace(self, pipeline=pipeline)

    @property
    def docs_dir(self) -> Path:
        return self.data_dir / "docs"

    @property
    def master_dir(self) -> Path:
        return self.data_dir / "master"

    @property
    def index_dir(self) -> Path:
        return self.data_dir / "index"

    @property
    def embedding_cache(self) -> Path:
        return self.index_dir / "embedding_cache.jsonl"

    @property
    def replay_dir(self) -> Path:
        return self.data_dir / "replay"

    @property
    def gold_set(self) -> Path:
        return self.data_dir / "gold_set.json"

    @property
    def query_log(self) -> Path:
        return self.out_dir / "query_log.db"

    @property
    def quotes_dir(self) -> Path:
        return self.out_dir / "quotes"

    @property
    def label(self) -> str:
        return f"{self.mode}:{self.pipeline}:{self.chat_provider}/{self.chat_model}"
