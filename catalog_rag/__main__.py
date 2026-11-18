"""Command line: python -m catalog_rag {ingest,ask,web,bot,eval}."""
from __future__ import annotations

import argparse
import sys

from .catalog import Filters
from .config import PIPELINES, ROLES, Settings


def cmd_ingest(settings: Settings) -> None:
    from .index import ingest
    from .runtime import build_embedder

    report = ingest(settings, build_embedder(settings))
    kinds = "、".join(f"{k} {v}" for k, v in sorted(report.chunks.items()))
    print(f"已解析 {report.documents} 份文档，{sum(report.chunks.values())} 个片段（{kinds}），{report.products} 个产品")
    e = report.embedding
    if e.input_tokens:
        print(f"新计算向量：{e.input_tokens} tokens，{e.ms / 1000:.1f}s，估算 ${e.cost_usd:.5f}（{e.model}，{settings.embed_dim} 维）")
    else:
        print(f"全部向量来自缓存（{settings.embedding_cache.name}，{report.cached_vectors} 条），未调用接口")
    print(f"索引：{settings.index_dir}")


def cmd_ask(settings: Settings, args: argparse.Namespace) -> None:
    from .runtime import build_assistant

    filters = Filters(args.model, args.material, args.size, args.price_min, args.price_max)
    answer = build_assistant(settings).ask(args.question, args.role, filters, channel="cli")
    print(f"[{settings.label} · 角色 {args.role}]")
    print(answer.render())


def cmd_web(settings: Settings, port: int) -> None:
    import uvicorn

    from .web import create_app

    print(f"网页聊天：http://127.0.0.1:{port}  （{settings.label}；演示数据均为虚构）")
    uvicorn.run(create_app(settings), host="127.0.0.1", port=port, log_level="warning")


def cmd_eval(settings: Settings, pipeline: str) -> None:
    from .evaluate import render_markdown, run_eval

    for name in (PIPELINES if pipeline == "all" else (pipeline,)):
        report = run_eval(settings.with_pipeline(name))  # type: ignore[arg-type]
        print(render_markdown(report))
        print(f"评测报告：{report['report_path']}\n")


def main(argv: list[str] | None = None) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="python -m catalog_rag",
                                     description="产品目录 RAG 助手（飞书机器人 + 网页），演示数据均为虚构")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("ingest", help="解析 data/docs 下的规格书、价目表、FAQ，建立索引和向量缓存")
    ask = sub.add_parser("ask", help="提一个问题")
    ask.add_argument("question")
    ask.add_argument("--role", choices=ROLES, default="销售")
    ask.add_argument("--model", help="筛选：型号包含")
    ask.add_argument("--material", help="筛选：材质包含，如 316、FKM")
    ask.add_argument("--size", help="筛选：尺寸，如 M6、DN25")
    ask.add_argument("--price-min", type=float, help="筛选：目录价下限（仅销售）")
    ask.add_argument("--price-max", type=float, help="筛选：目录价上限（仅销售）")
    web = sub.add_parser("web", help="本地网页聊天（127.0.0.1）")
    web.add_argument("--port", type=int, default=8780)
    sub.add_parser("bot", help="飞书机器人（长连接，无需公网地址）")
    ev = sub.add_parser("eval", help="在 50 题评测集上评测")
    ev.add_argument("--pipeline", choices=(*PIPELINES, "all"), default="all")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    if args.command == "ingest":
        cmd_ingest(settings)
    elif args.command == "ask":
        cmd_ask(settings, args)
    elif args.command == "web":
        cmd_web(settings, args.port)
    elif args.command == "bot":
        from .feishu_bot import run_bot

        run_bot(settings)
    elif args.command == "eval":
        cmd_eval(settings, args.pipeline)


if __name__ == "__main__":
    main()
