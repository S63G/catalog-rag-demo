"""Query log (SQLite): who asked what, in which role, what was retrieved and cited, latency and cost per stage.

The asker is stored as a 12-character SHA-256 prefix of the channel user id, never the raw Feishu id.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS queries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    channel TEXT NOT NULL,
    user_hash TEXT NOT NULL,
    role TEXT NOT NULL,
    question TEXT NOT NULL,
    filters TEXT NOT NULL,
    intent TEXT NOT NULL,
    pipeline TEXT NOT NULL,
    status TEXT NOT NULL,
    answer TEXT NOT NULL,
    citations TEXT NOT NULL,
    retrieved TEXT NOT NULL,
    warnings TEXT NOT NULL,
    trace TEXT NOT NULL,
    total_ms REAL NOT NULL,
    cost_usd REAL NOT NULL
)
"""


def user_hash(user: str) -> str:
    return hashlib.sha256(user.encode("utf-8")).hexdigest()[:12]


class QueryLog:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as db, db:
            db.execute(SCHEMA)

    def write(self, *, channel: str, user: str, role: str, question: str, filters: dict, intent: str,
              pipeline: str, status: str, answer: str, citations: list[dict], retrieved: list[dict],
              warnings: list[str], trace: dict) -> int:
        row = (dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), channel, user_hash(user), role,
               question, json.dumps(filters, ensure_ascii=False), intent, pipeline, status, answer,
               json.dumps(citations, ensure_ascii=False), json.dumps(retrieved, ensure_ascii=False),
               json.dumps(warnings, ensure_ascii=False), json.dumps(trace, ensure_ascii=False),
               trace.get("total_ms", 0.0), trace.get("total_cost_usd", 0.0))
        with closing(sqlite3.connect(self.path)) as db, db:
            cur = db.execute(
                "INSERT INTO queries (ts, channel, user_hash, role, question, filters, intent, pipeline, status, "
                "answer, citations, retrieved, warnings, trace, total_ms, cost_usd) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", row)
            return int(cur.lastrowid or 0)

    def recent(self, limit: int = 50) -> list[dict]:
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute("SELECT * FROM queries ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            for key in ("filters", "citations", "retrieved", "warnings", "trace"):
                item[key] = json.loads(item[key])
            out.append(item)
        return out
