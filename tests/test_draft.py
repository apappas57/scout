import json
from scout.models import Role, Score
from scout.llm import FakeLLMClient
from scout.draft import draft_role

DRAFT_JSON = json.dumps({"cover_letter": "Dear team, ...", "resume_note": "Lead with Optible.", "warm_path": "Andrew C."})
CRIT_OK = json.dumps({"ok": True, "issues": []})


def test_draft_populates_and_passes_critique():
    role = Role("Cresta", "Senior FDE", "u", "Remote", score=Score.HIGH)
    out = draft_role(FakeLLMClient([DRAFT_JSON, CRIT_OK]), role, profile={"name": "Alex"})
    assert out.drafts["cover_letter"].startswith("Dear team")
    assert "_flag" not in out.drafts


def test_failed_critique_triggers_one_revision():
    crit_bad = json.dumps({"ok": False, "issues": ["too generic"]})
    revised = json.dumps({"cover_letter": "Specific opener about Optible.", "resume_note": "x", "warm_path": ""})
    client = FakeLLMClient([DRAFT_JSON, crit_bad, revised, CRIT_OK])
    out = draft_role(client, Role("X", "FDE", "u", "R", score=Score.HIGH), profile={})
    assert "Specific opener" in out.drafts["cover_letter"]
