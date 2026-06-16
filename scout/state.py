from __future__ import annotations
import json
import sqlite3
from pathlib import Path


def seen_ids(conn: sqlite3.Connection) -> set[str]:
    """Return the set of role ids already persisted in the roles table."""
    return {row[0] for row in conn.execute("select id from roles")}


def record_audit(
    conn: sqlite3.Connection,
    run_id: str,
    stage: str,
    action: str,
    role_id: str | None = None,
    detail: str = "",
) -> None:
    """Append a single audit-trail entry for a run stage."""
    ts = _audit_ts(conn)
    conn.execute(
        "insert into audit(run_id, stage, role_id, action, detail, ts) "
        "values(?,?,?,?,?,?)",
        (run_id, stage, role_id, action, detail, ts),
    )
    conn.commit()


def _audit_ts(conn: sqlite3.Connection) -> str:
    """Best-effort timestamp for an audit row. Uses sqlite's clock so audit
    rows are always stamped even when a caller does not pass one."""
    return conn.execute("select strftime('%Y-%m-%dT%H:%M:%fZ','now')").fetchone()[0]


def start_run(conn: sqlite3.Connection, run_id: str, ts: str) -> None:
    """Open a run row in the 'running' state."""
    conn.execute(
        "insert into runs(run_id, started_at, status) values(?,?,'running')",
        (run_id, ts),
    )
    conn.commit()


def finish_run(
    conn: sqlite3.Connection,
    run_id: str,
    ts: str,
    counts: dict,
    status: str,
    error: str | None,
) -> None:
    """Close out a run row with final counts, status, and any error."""
    conn.execute(
        "update runs set finished_at=?, status=?, counts_json=?, error=? where run_id=?",
        (ts, status, json.dumps(counts), error, run_id),
    )
    conn.commit()


def write_heartbeat(path, run_id: str, ts: str) -> None:
    """Write a one-line heartbeat file so the launchd wrapper can confirm a run."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"{run_id} {ts}")
