"""Local web chat (FastAPI, 127.0.0.1): ask, view sources and traces, open cited documents, download quote drafts."""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .assistant import Assistant
from .catalog import Filters
from .config import ROLES, Settings
from .index import doc_path, docs_listing
from .runtime import build_assistant

STATIC = Path(__file__).parent / "static"


class FilterIn(BaseModel):
    model: str | None = None
    material: str | None = None
    size: str | None = None
    price_min: float | None = None
    price_max: float | None = None


class AskIn(BaseModel):
    question: str = Field(..., min_length=1, max_length=500)
    role: str = "销售"
    filters: FilterIn = Field(default_factory=FilterIn)


def create_app(settings: Settings, assistant: Assistant | None = None) -> FastAPI:
    assistant = assistant or build_assistant(settings)
    index = assistant.index
    app = FastAPI(title="产品目录 RAG 助手（演示数据均为虚构）", docs_url=None, redoc_url=None)

    @app.get("/")
    def home() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/api/info")
    def info() -> dict:
        return {"label": settings.label, "roles": list(ROLES), "documents": docs_listing(index),
                "products": len(index.catalog.products), "chunks": len(index.chunks)}

    @app.post("/api/ask")
    def ask(body: AskIn) -> dict:
        if body.role not in ROLES:
            raise HTTPException(400, f"role must be one of {ROLES}")
        filters = Filters(**{k: (v or None) if isinstance(v, str) else v for k, v in body.filters.model_dump().items()})
        return assistant.ask(body.question, body.role, filters, channel="web", user="web").to_dict()

    @app.get("/api/chunk/{chunk_id:path}")
    def chunk(chunk_id: str, role: str = "客服") -> dict:
        c = index.by_id.get(chunk_id)
        if c is None or (c.sales_only and role != "销售"):
            raise HTTPException(404, "not found")
        return {"id": c.id, "doc": c.doc, "locator": c.locator, "page": c.page, "label": c.cite(), "text": c.text}

    @app.get("/api/logs")
    def logs(limit: int = 30) -> list[dict]:
        return [{k: r[k] for k in ("id", "ts", "channel", "role", "question", "intent", "status", "total_ms",
                                   "cost_usd")} | {"citations": len(r["citations"])}
                for r in assistant.log.recent(min(limit, 200))]

    @app.get("/files/{name}")
    def document(name: str, role: str = "客服") -> FileResponse:
        path = doc_path(settings, name)
        if path is None or (path.suffix == ".xlsx" and role != "销售"):
            raise HTTPException(404, "not found")
        return FileResponse(path, filename=path.name if path.suffix != ".pdf" else None)

    @app.get("/quotes/{name}")
    def quote_file(name: str) -> FileResponse:
        path = (settings.quotes_dir / name).resolve()
        if path.parent != settings.quotes_dir.resolve() or not path.is_file():
            raise HTTPException(404, "not found")
        return FileResponse(path, filename=path.name)

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
