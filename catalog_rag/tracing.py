"""Per-answer trace: latency and token usage of every stage, and an estimated cost.

Unit prices (USD per 1M tokens) and where they come from, as checked on 2026-11-18:
- deepseek-flash: DeepSeek pricing page (api-docs.deepseek.com/quick_start/pricing): input cache miss 0.15, cache hit
  0.003, output 0.6 off-peak; peak hours (01:00-04:00 and 06:00-10:00 UTC, Mon-Fri) are double.
- text-embedding-v4: Alibaba Cloud Model Studio docs, Beijing region, 0.072.
- gte-rerank-v2: 0.115, quoted by a third-party listing of Beijing-region prices (not confirmed on an official page).
- qwen-plus: no official figure found; estimated from its earlier public price (CNY 0.8 / 2.0 per 1M input / output
  tokens) at 7.1 CNY per USD.
Real spend should be read from the provider consoles; the README reports both.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass, field

# model -> (input, cached input, output) USD per 1M tokens, off-peak where applicable
PRICES_USD_PER_M: dict[str, tuple[float, float, float]] = {
    "deepseek-flash": (0.15, 0.003, 0.6),
    "deepseek-v4-pro": (0.66, 0.022, 1.98),
    "qwen-plus": (0.8 / 7.1, 0.8 / 7.1, 2.0 / 7.1),
    "text-embedding-v4": (0.072, 0.072, 0.0),
    "gte-rerank-v2": (0.115, 0.115, 0.0),
}


def deepseek_peak(when: dt.datetime) -> bool:
    utc = when.astimezone(dt.timezone.utc)
    return utc.weekday() < 5 and (1 <= utc.hour < 4 or 6 <= utc.hour < 10)


def cost_usd(model: str, input_tokens: int, output_tokens: int, cached_tokens: int = 0,
             when: dt.datetime | None = None) -> float:
    price = PRICES_USD_PER_M.get(model)
    if price is None:
        return 0.0
    p_in, p_cached, p_out = price
    factor = 2.0 if model.startswith("deepseek") and when is not None and deepseek_peak(when) else 1.0
    fresh = max(0, input_tokens - cached_tokens)
    return factor * (fresh * p_in + cached_tokens * p_cached + output_tokens * p_out) / 1_000_000


@dataclass
class Stage:
    name: str                 # bm25 | embed_query | vector | rerank | generate | quote_parse | ...
    ms: float
    model: str | None = None
    provider: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cost_usd: float = 0.0
    replayed: bool = False    # latency is the one recorded during the live run


@dataclass
class Trace:
    stages: list[Stage] = field(default_factory=list)

    def add(self, stage: Stage) -> Stage:
        self.stages.append(stage)
        return stage

    @property
    def total_ms(self) -> float:
        return round(sum(s.ms for s in self.stages), 1)

    @property
    def total_cost_usd(self) -> float:
        return sum(s.cost_usd for s in self.stages)

    def tokens_by_provider(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for s in self.stages:
            if s.provider:
                t = out.setdefault(s.provider, {"input": 0, "output": 0, "cached": 0, "calls": 0})
                t["input"] += s.input_tokens
                t["output"] += s.output_tokens
                t["cached"] += s.cached_tokens
                t["calls"] += 1
        return out

    def to_dict(self) -> dict:
        return {"stages": [asdict(s) for s in self.stages], "total_ms": self.total_ms,
                "total_cost_usd": round(self.total_cost_usd, 8)}

    def summary(self) -> str:
        models = sorted({s.model for s in self.stages if s.model})
        replay = "（回放：延迟为录制时实测）" if any(s.replayed for s in self.stages) else ""
        return f"耗时 {self.total_ms / 1000:.1f}s · 估算费用 ${self.total_cost_usd:.5f} · {'、'.join(models) or '无模型调用'}{replay}"
