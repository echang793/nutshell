"""SQLite / Turso persistence layer.

Set TURSO_URL + TURSO_TOKEN env vars to use Turso (production).
Falls back to local SQLite when those vars are absent (dev).
"""

from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "summaries.db"

_SCHEMA_STMTS = [
    """CREATE TABLE IF NOT EXISTS summaries (
        id          TEXT    PRIMARY KEY,
        video_id    TEXT    NOT NULL,
        url         TEXT    NOT NULL,
        title       TEXT    NOT NULL DEFAULT '',
        thumbnail_url TEXT  NOT NULL DEFAULT '',
        notes       TEXT    NOT NULL,
        brief       INTEGER NOT NULL DEFAULT 0,
        word_count  INTEGER NOT NULL DEFAULT 0,
        mode        TEXT    NOT NULL DEFAULT 'general',
        pinned      INTEGER NOT NULL DEFAULT 0,
        created_at  TEXT    NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_created ON summaries(created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_video   ON summaries(video_id)",
    """CREATE TABLE IF NOT EXISTS groq_usage (
        date   TEXT    PRIMARY KEY,
        tokens INTEGER NOT NULL DEFAULT 0
    )""",
]

# Additive migrations for columns added after the table first existed in prod.
# CREATE TABLE IF NOT EXISTS never alters an existing table, so these run every
# connection and no-op (duplicate-column error, swallowed) once already applied.
_MIGRATION_COLUMNS = [
    ("title",         "TEXT NOT NULL DEFAULT ''"),
    ("thumbnail_url", "TEXT NOT NULL DEFAULT ''"),
    ("mode",          "TEXT NOT NULL DEFAULT 'general'"),
    ("pinned",        "INTEGER NOT NULL DEFAULT 0"),
]


def _run_migrations(conn) -> None:
    for col, ddl in _MIGRATION_COLUMNS:
        try:
            conn.execute(f"ALTER TABLE summaries ADD COLUMN {col} {ddl}")
        except Exception:
            pass  # column already exists


# ── Turso HTTP client ──────────────────────────────────────────────────

def _turso_arg(v):
    if v is None:
        return {"type": "null"}
    if isinstance(v, bool):
        return {"type": "integer", "value": "1" if v else "0"}
    if isinstance(v, int):
        return {"type": "integer", "value": str(v)}
    if isinstance(v, float):
        return {"type": "float", "value": str(v)}
    return {"type": "text", "value": str(v)}


def _turso_val(v: dict):
    t = v.get("type", "null")
    if t == "null":
        return None
    if t == "integer":
        return int(v["value"])
    if t == "float":
        return float(v["value"])
    return v.get("value")


class _TursoRow(dict):
    """dict subclass that supports row["col"] access, compatible with sqlite3.Row."""
    pass


class _TursoCursor:
    def __init__(self, cols: list[str], rows: list):
        self._rows = [
            _TursoRow(zip(cols, [_turso_val(v) for v in row]))
            for row in rows
        ]

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


class _TursoConn:
    """Pure-HTTP Turso client. No native compilation required."""

    def __init__(self, url: str, token: str):
        import requests as _req
        self._sess  = _req.Session()
        self._sess.headers.update({
            "Authorization": f"Bearer {token}",
            "Content-Type":  "application/json",
        })
        self._url = url.strip().replace("libsql://", "https://") + "/v2/pipeline"

    def execute(self, sql: str, params=()):
        body = {"requests": [
            {"type": "execute", "stmt": {
                "sql":  sql,
                "args": [_turso_arg(p) for p in params],
            }},
            {"type": "close"},
        ]}
        r = self._sess.post(self._url, json=body, timeout=15)
        r.raise_for_status()
        data   = r.json()
        result = data["results"][0]
        if result.get("type") == "error":
            raise Exception(result.get("error", {}).get("message", "Turso error"))
        res    = result.get("response", {}).get("result", {})
        cols   = [c["name"] for c in res.get("cols", [])]
        rows   = res.get("rows", [])
        return _TursoCursor(cols, rows)

    def commit(self):   pass   # HTTP API is auto-commit per request
    def rollback(self): pass
    def __enter__(self):        return self
    def __exit__(self, *args):  return False


# ── Connection factory ─────────────────────────────────────────────────

_turso: _TursoConn | None = None
_turso_lock = threading.Lock()


def _conn():
    global _turso
    turso_url   = os.environ.get("TURSO_URL", "").strip()
    turso_token = os.environ.get("TURSO_TOKEN", "").strip()

    if turso_url and turso_token:
        if _turso is None:
            with _turso_lock:
                if _turso is None:   # double-checked locking
                    conn = _TursoConn(turso_url, turso_token)
                    for stmt in _SCHEMA_STMTS:
                        conn.execute(stmt)
                    _run_migrations(conn)
                    _turso = conn
        return _turso

    # Local dev — plain SQLite
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    inner = sqlite3.connect(str(DB_PATH))
    inner.row_factory = sqlite3.Row
    for stmt in _SCHEMA_STMTS:
        inner.execute(stmt)
    _run_migrations(inner)
    inner.commit()
    return inner


_MAX_SUMMARIES = 1000


def save_summary(id: str, video_id: str, url: str, notes: str,
                 brief: bool, word_count: int,
                 title: str = "", thumbnail_url: str = "", mode: str = "general") -> None:
    with _conn() as c:
        c.execute(
            """INSERT OR REPLACE INTO summaries
               (id, video_id, url, title, thumbnail_url, notes, brief, word_count, mode, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (id, video_id, url, title, thumbnail_url, notes, int(brief), word_count,
             mode, datetime.now().isoformat()),
        )
        c.execute(
            """DELETE FROM summaries WHERE id IN (
               SELECT id FROM summaries ORDER BY created_at DESC
               LIMIT -1 OFFSET ?)""",
            (_MAX_SUMMARIES,),
        )


def get_summary(id: str) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM summaries WHERE id=?", (id,)).fetchone()
    if not row:
        return None
    return _row_to_dict(row)


def get_history(limit: int = 50) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM summaries ORDER BY pinned DESC, created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def delete_summary(id: str) -> None:
    with _conn() as c:
        c.execute("DELETE FROM summaries WHERE id=?", (id,))


def set_pinned(id: str, pinned: bool) -> None:
    with _conn() as c:
        c.execute("UPDATE summaries SET pinned=? WHERE id=?", (int(pinned), id))


def clear_history() -> None:
    with _conn() as c:
        c.execute("DELETE FROM summaries")


def get_monthly_summary_count() -> int:
    month_prefix = datetime.now().strftime("%Y-%m")
    with _conn() as c:
        row = c.execute(
            "SELECT COUNT(*) AS n FROM summaries WHERE created_at >= ?",
            (month_prefix + "-01",),
        ).fetchone()
    return int(row["n"]) if row else 0


def record_groq_usage(tokens: int) -> None:
    today = datetime.now().strftime("%Y-%m-%d")
    with _conn() as c:
        c.execute(
            "INSERT INTO groq_usage (date, tokens) VALUES (?, ?) "
            "ON CONFLICT(date) DO UPDATE SET tokens = tokens + excluded.tokens",
            (today, tokens),
        )


def set_groq_usage_today(tokens: int) -> None:
    """Resync to Groq's authoritative usage figure (parsed from a 429 error body).
    Our own counter only sees calls made through this app, so it can undercount
    if other traffic shares the same Groq account — take the max, never regress."""
    today = datetime.now().strftime("%Y-%m-%d")
    with _conn() as c:
        c.execute(
            "INSERT INTO groq_usage (date, tokens) VALUES (?, ?) "
            "ON CONFLICT(date) DO UPDATE SET tokens = MAX(tokens, excluded.tokens)",
            (today, tokens),
        )


def get_groq_usage_today() -> int:
    today = datetime.now().strftime("%Y-%m-%d")
    with _conn() as c:
        row = c.execute(
            "SELECT tokens FROM groq_usage WHERE date=?", (today,)
        ).fetchone()
    return int(row["tokens"]) if row else 0


def _row_to_dict(row) -> dict:
    d = dict(row)
    d["brief"]  = bool(d.get("brief", 0))
    d["pinned"] = bool(d.get("pinned", 0))
    return d
