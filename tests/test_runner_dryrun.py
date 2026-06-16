import json
from datetime import datetime, timezone
from scout import runner
from scout.llm import FakeLLMClient

NOW = datetime(2026, 6, 16, tzinfo=timezone.utc).isoformat()
DISCOVER = json.dumps([
    {"company": "Anthropic", "title": "Forward Deployed Engineer", "url": "https://a/1", "location": "Remote AU"},
    {"company": "Beta", "title": "AI Engineer", "url": "https://b/2", "location": "Sydney"},
])
CONFIG = {
    "enabled": True,
    "search": {"queries": ["fde"], "target_companies": ["Anthropic"], "boards": []},
    "filters": {"remote_only": True, "allow_locations": ["Remote"], "exclude_locations": ["Sydney"], "exclude_keywords": []},
    "scoring": {"high_companies": ["Anthropic"], "high_keywords": ["forward deployed"], "seniority_keywords": ["senior"]},
    "profile": {"name": "Alex"},
    "notify": {"follow_up_days": 10, "ntfy_topic": ""},
}


def test_dry_run_discovers_filters_scores_without_writing(conn):
    fetch = lambda u: (200, "Apply now. Remote.")
    written, notified = [], []
    result = runner.run(config=CONFIG, conn=conn, client=FakeLLMClient([DISCOVER]),
                        now=NOW, fetch=fetch, write=lambda p, t: written.append(p),
                        notify=lambda t: notified.append(t), dry_run=True)
    assert result.discovered == 2
    assert result.new_qualified == 1      # Sydney dropped
    assert result.high == 1               # Anthropic
    assert conn.execute("select count(*) from roles").fetchone()[0] == 0  # dry-run wrote nothing
    assert written == [] and notified == []
