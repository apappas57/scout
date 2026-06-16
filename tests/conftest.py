import sqlite3
import pytest
from scout import db


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "scout.db"
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    db.init_schema(c)
    yield c
    c.close()
