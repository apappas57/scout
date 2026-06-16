from scout import db


def test_schema_has_core_tables(conn):
    names = {r[0] for r in conn.execute("select name from sqlite_master where type='table'")}
    assert {"roles", "runs", "audit"} <= names


def test_connect_creates_file(tmp_path):
    c = db.connect(tmp_path / "scout.db")
    assert {r[0] for r in c.execute("select name from sqlite_master where type='table'")} >= {"roles"}
