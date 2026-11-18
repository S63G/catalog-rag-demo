"""Evaluate one pipeline on the 50-question gold set (data/gold_set.json).

Metrics: hit@5 (an accepted source among the top-5 retrieved chunks), answer correctness (required points present,
forbidden ones absent; quote lines and total exact; refusals / permission denials as expected), citation correctness
(the answer cites an accepted source), refusal correctness, ungrounded numbers, and latency / cost per question.
Live runs are written to evaluation/, replay runs to out/eval_replay/ so they never overwrite measured results.
"""
from __future__ import annotations

import datetime as dt
import json
import statistics
from dataclasses import replace

from .assistant import Answer
from .catalog import Filters
from .config import PROJECT_ROOT, Settings
from .runtime import build_assistant
from .text import compact

STATUS_ZH = {"answered": "已回答", "refused": "拒答", "denied": "无权限", "quote": "报价草稿", "clarify": "需澄清",
             "error": "出错"}


def _includes(text: str, item: str | list[str]) -> bool:
    options = item if isinstance(item, list) else [item]
    return any(compact(o) in text for o in options)


def _check_quote(answer: Answer, expected: dict) -> tuple[bool, str]:
    q = answer.quote
    if answer.status != "quote" or q is None:
        return False, f"状态 {answer.status}"
    if not q.customer or q.customer.id != expected["customer_id"]:
        return False, f"客户 {q.customer.id if q.customer else None}"
    got = sorted((ln.product.sku, ln.quantity, ln.unit_price) for ln in q.lines)
    want = sorted((ln["sku"], float(ln["qty"]), float(ln["unit_price"])) for ln in expected["lines"])
    if got != want:
        return False, f"行 {got} ≠ {want}"
    if abs(q.total - expected["total"]) > 0.005:
        return False, f"合计 {q.total} ≠ {expected['total']}"
    return True, ""


def score_question(q: dict, answer: Answer) -> dict:
    retrieved = [h.chunk.source_key for h in answer.hits[:5]]
    cited = [f"{c.doc}|{c.locator}" for c in answer.citations]
    sources = set(q.get("sources", []))
    text = compact(answer.text)
    row: dict = {"id": q["id"], "category": q["category"], "role": q["role"], "question": q["question"],
                 "expect": q["expect"], "status": answer.status, "answer": answer.text,
                 "sources": sorted(sources), "cited": cited,
                 "retrieved_top5": retrieved, "warnings": answer.warnings, "total_ms": answer.trace.total_ms,
                 "cost_usd": answer.trace.total_cost_usd, "tokens": answer.trace.tokens_by_provider(),
                 "hit5": None, "citation_ok": None, "reason": ""}
    if q["expect"] == "answer" and "quote" in q:
        row["correct"], row["reason"] = _check_quote(answer, q["quote"])
        return row
    if q["expect"] in ("refuse", "deny"):
        wanted = "refused" if q["expect"] == "refuse" else "denied"
        row["correct"] = answer.status == wanted
        row["reason"] = "" if row["correct"] else f"期望{STATUS_ZH[wanted]}，实际{STATUS_ZH[answer.status]}"
        return row
    row["hit5"] = bool(sources.intersection(retrieved))
    row["citation_ok"] = answer.status == "answered" and bool(sources.intersection(cited))
    missing = [m if isinstance(m, str) else "/".join(m) for m in q["must_include"] if not _includes(text, m)]
    forbidden = [m for m in q.get("must_not_include", []) if _includes(text, m)]
    row["correct"] = answer.status == "answered" and not missing and not forbidden
    if answer.status != "answered":
        row["reason"] = f"期望回答，实际{STATUS_ZH[answer.status]}"
    elif missing or forbidden:
        row["reason"] = "缺少：" + "、".join(missing) if missing else ""
        row["reason"] += ("；不应出现：" + "、".join(forbidden)) if forbidden else ""
    return row


def _ratio(n: int, d: int) -> dict:
    return {"n": n, "d": d, "pct": round(100 * n / d, 1) if d else None}


def summarize(rows: list[dict]) -> dict:
    answerable = [r for r in rows if r["expect"] == "answer"]
    retrieval = [r for r in answerable if r["hit5"] is not None]
    refuse = [r for r in rows if r["expect"] == "refuse"]
    deny = [r for r in rows if r["expect"] == "deny"]
    cited_total = sum(len(r["cited"]) for r in retrieval)
    latencies = [r["total_ms"] for r in rows if r["tokens"]]
    tokens: dict[str, dict[str, int]] = {}
    for r in rows:
        for provider, t in r["tokens"].items():
            agg = tokens.setdefault(provider, {"input": 0, "output": 0, "cached": 0, "calls": 0})
            for key in agg:
                agg[key] += t[key]
    categories: dict[str, list[int]] = {}
    for r in rows:
        c = categories.setdefault(r["category"], [0, 0])
        c[0] += int(r["correct"])
        c[1] += 1
    return {
        "questions": len(rows),
        "overall_correct": _ratio(sum(r["correct"] for r in rows), len(rows)),
        "hit_at_5": _ratio(sum(r["hit5"] for r in retrieval), len(retrieval)),
        "answer_correct": _ratio(sum(r["correct"] for r in answerable), len(answerable)),
        "citation_correct": _ratio(sum(r["citation_ok"] for r in retrieval), len(retrieval)),
        "citation_precision": _ratio(sum(1 for r in retrieval for c in r["cited"] if c in r["sources"]), cited_total),
        "refusal_correct": _ratio(sum(r["correct"] for r in refuse), len(refuse)),
        "false_refusal": _ratio(sum(r["status"] in ("refused", "denied") for r in answerable), len(answerable)),
        "deny_correct": _ratio(sum(r["correct"] for r in deny), len(deny)),
        "price_leak_to_support": sum(1 for r in rows if r["role"] == "客服"
                                     for c in r["cited"] if "价目表" in c),
        "ungrounded_number_answers": sum(1 for r in rows if any("没有出现在引用资料里" in w for w in r["warnings"])),
        "errors": sum(r["status"] == "error" for r in rows),
        "latency_ms": {"mean": round(statistics.mean(latencies), 1), "p50": round(statistics.median(latencies), 1),
                       "p95": round(_p95(latencies), 1), "max": round(max(latencies), 1),
                       "n": len(latencies)} if latencies else None,
        "cost_usd": {"total": round(sum(r["cost_usd"] for r in rows), 6),
                     "per_question": round(sum(r["cost_usd"] for r in rows) / len(rows), 7)},
        "tokens": tokens,
        "by_category": {k: _ratio(v[0], v[1]) for k, v in categories.items()},
    }


def _p95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(0.95 * (len(ordered) - 1)))]


def run_eval(settings: Settings) -> dict:
    gold = json.loads(settings.gold_set.read_text(encoding="utf-8"))
    run_dir = settings.out_dir / "eval_runs" / settings.pipeline
    assistant = build_assistant(replace(settings, out_dir=run_dir))
    rows = []
    for q in gold["questions"]:
        answer = assistant.ask(q["question"], q["role"], Filters(**q.get("filters", {})), channel="eval")
        rows.append(score_question(q, answer))
    finished = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    report = {"label": settings.label, "pipeline": settings.pipeline, "mode": settings.mode,
              "chat_model": f"{settings.chat_provider}/{settings.chat_model}", "embed_model":
              f"{settings.embed_model} ({settings.embed_dim}d)", "rerank_model": settings.rerank_model
              if settings.pipeline == "hybrid_rerank" else None, "run_at": finished,
              "gold_written_at": gold["written_at"], "summary": summarize(rows), "rows": rows}
    folder = PROJECT_ROOT / "evaluation" if settings.mode == "live" else settings.out_dir / "eval_replay"
    folder.mkdir(parents=True, exist_ok=True)
    stem = f"{settings.pipeline}__{settings.chat_model}"
    (folder / f"{stem}.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    md_path = folder / f"{stem}.md"
    md_path.write_text(render_markdown(report), encoding="utf-8")
    report["report_path"] = str(md_path)
    return report


def _fmt(r: dict) -> str:
    return "—" if not r["d"] else f"{r['n']}/{r['d']} = {r['pct']}%"


def render_markdown(report: dict) -> str:
    s = report["summary"]
    lat = s["latency_ms"] or {}
    lines = [
        f"## 评测：{report['pipeline']} · {report['chat_model']} · {report['mode']}",
        "",
        f"运行时间 {report['run_at']}（UTC）；题目写定于 {report['gold_written_at']}；向量 {report['embed_model']}；"
        f"重排 {report['rerank_model'] or '无'}。",
        "",
        "| 指标 | 结果 |",
        "|---|---|",
        f"| 总体正确（50 题） | {_fmt(s['overall_correct'])} |",
        f"| hit@5（应答题，不含报价） | {_fmt(s['hit_at_5'])} |",
        f"| 回答正确率（应答题，含报价） | {_fmt(s['answer_correct'])} |",
        f"| 引用正确率（引用了可接受的来源） | {_fmt(s['citation_correct'])} |",
        f"| 引用精确率（引用中属于可接受来源的比例） | {_fmt(s['citation_precision'])} |",
        f"| 应拒答且拒答 | {_fmt(s['refusal_correct'])} |",
        f"| 误拒答（应答题被拒答） | {_fmt(s['false_refusal'])} |",
        f"| 权限拒绝正确（客服问价/报价） | {_fmt(s['deny_correct'])} |",
        f"| 客服回答引用价目表的次数（目标 0） | {s['price_leak_to_support']} |",
        f"| 回答中含资料外数字的题数 | {s['ungrounded_number_answers']} |",
        f"| 调用出错 | {s['errors']} |",
        f"| 延迟（调用了模型的 {lat.get('n', 0)} 题）均值 / P50 / P95 | "
        f"{lat.get('mean', 0) / 1000:.2f}s / {lat.get('p50', 0) / 1000:.2f}s / {lat.get('p95', 0) / 1000:.2f}s |",
        f"| 估算费用 合计 / 每题 | ${s['cost_usd']['total']:.4f} / ${s['cost_usd']['per_question']:.5f} |",
        "",
        "| 提供方 | 调用次数 | 输入 tokens | 其中缓存命中 | 输出 tokens |",
        "|---|---|---|---|---|",
    ]
    lines += [f"| {p} | {t['calls']} | {t['input']:,} | {t['cached']:,} | {t['output']:,} |"
              for p, t in sorted(s["tokens"].items())]
    lines += ["", "| 类别 | 正确 |", "|---|---|"]
    lines += [f"| {k} | {_fmt(v)} |" for k, v in s["by_category"].items()]
    wrong = [r for r in report["rows"] if not r["correct"] or r["hit5"] is False or r["citation_ok"] is False]
    if wrong:
        lines += ["", "未完全正确的题：", "", "| 题号 | 问题 | 结果 | hit@5 | 引用 | 说明 |", "|---|---|---|---|---|---|"]
        for r in wrong:
            hit = {None: "—", True: "✓", False: "✗"}[r["hit5"]]
            cite = {None: "—", True: "✓", False: "✗"}[r["citation_ok"]]
            mark = "✓" if r["correct"] else "✗"
            lines.append(f"| {r['id']} | {r['question']} | {mark} {STATUS_ZH[r['status']]} | {hit} | {cite} | "
                         f"{r['reason'] or '答案正确'} |")
    return "\n".join(lines) + "\n"
