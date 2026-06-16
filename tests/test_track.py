import json
from datetime import datetime, timezone, timedelta
from scout import track
from scout.models import Role, Score


def _t(days_ago=0):
    return (datetime(2026, 6, 16, tzinfo=timezone.utc) - timedelta(days=days_ago)).isoformat()


def test_upsert_is_idempotent(conn):
    r = Role("A", "FDE", "https://a/1", "Remote", score=Score.HIGH)
    track.upsert_roles(conn, [r], now=_t())
    track.upsert_roles(conn, [r], now=_t())   # re-run, no dupes
    assert conn.execute("select count(*) from roles").fetchone()[0] == 1


def test_stale_application_gets_follow_up(conn):
    conn.execute("insert into roles(id,company,title,url,status,applied_at,first_seen_at,last_seen_at) "
                 "values('z','C','FDE','u','applied',?,?,?)", (_t(15), _t(20), _t(15)))
    conn.commit()
    stale = track.stale_applications(conn, now=_t(), follow_up_days=10)
    assert [r.company for r in stale] == ["C"]
    again = track.stale_applications(conn, now=_t(), follow_up_days=10)  # not surfaced twice
    assert again == []
