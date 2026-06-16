from scout import state
from datetime import datetime, timezone


def _now():
    return datetime(2026, 6, 16, 9, 0, tzinfo=timezone.utc).isoformat()


def test_seen_ids_roundtrip(conn):
    assert state.seen_ids(conn) == set()
    conn.execute(
        "insert into roles(id,company,title,url,first_seen_at,last_seen_at,status) "
        "values('abc','A','T','u',?,?,'new')", (_now(), _now()))
    conn.commit()
    assert "abc" in state.seen_ids(conn)


def test_run_lifecycle_and_audit(conn):
    state.start_run(conn, "r1", _now())
    state.record_audit(conn, "r1", "discover", "found", role_id="x", detail="Acme FDE")
    state.finish_run(conn, "r1", _now(), counts={"discovered": 1}, status="ok", error=None)
    row = conn.execute("select status, counts_json from runs where run_id='r1'").fetchone()
    assert row["status"] == "ok" and '"discovered": 1' in row["counts_json"]
    assert conn.execute("select count(*) from audit where run_id='r1'").fetchone()[0] == 1
