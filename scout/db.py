from __future__ import annotations
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS roles (
    id TEXT PRIMARY KEY,
    company TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    location TEXT,
    source TEXT,
    snippet TEXT,
    workplace TEXT,
    description TEXT,
    score TEXT,
    score_reason TEXT,
    status TEXT NOT NULL DEFAULT 'new',
    drafts_json TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    applied_at TEXT,
    follow_up_due TEXT,
    notes TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    counts_json TEXT,
    error TEXT
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    role_id TEXT,
    action TEXT NOT NULL,
    detail TEXT,
    ts TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_roles_status ON roles(status);
CREATE INDEX IF NOT EXISTS idx_audit_run ON audit(run_id);
"""


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    # Columns added after v1. CREATE TABLE IF NOT EXISTS never upgrades an
    # existing table, so a database created before these columns existed gets
    # them added here. Idempotent: guarded by what the table actually has.
    existing = {row[1] for row in conn.execute("PRAGMA table_info(roles)")}
    for column in ("workplace", "description"):
        if column not in existing:
            conn.execute(f"ALTER TABLE roles ADD COLUMN {column} TEXT")
    conn.commit()


def connect(path) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    return conn
