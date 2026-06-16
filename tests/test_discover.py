import json
from pathlib import Path
from scout.llm import FakeLLMClient
from scout.discover import discover

FIX = Path(__file__).resolve().parent.parent / "fixtures" / "discover_sample.json"


def test_discover_parses_roles_from_llm_json():
    fake = FakeLLMClient([FIX.read_text()])
    roles = discover(fake, search={"queries": ["fde"], "target_companies": ["Anthropic"], "boards": []})
    assert len(roles) >= 3
    assert roles[0].company and roles[0].url.startswith("http")
    assert fake.calls[0].web is True   # discover must use web


def test_discover_tolerates_prose_around_json():
    payload = 'Here are roles:\n[{"company":"A","title":"FDE","url":"https://a/1","location":"Remote"}]\nDone.'
    roles = discover(FakeLLMClient([payload]), search={"queries": [], "target_companies": [], "boards": []})
    assert roles[0].company == "A"


def test_discover_batches_per_company_and_dedupes():
    r1 = json.dumps([{"company": "Anthropic", "title": "FDE", "url": "https://a/1", "location": "Remote"}])
    r2 = json.dumps([
        {"company": "Cresta", "title": "FDE", "url": "https://c/2", "location": "Remote AU"},
        {"company": "Anthropic", "title": "FDE", "url": "https://a/1", "location": "Remote"},  # dup of r1
    ])
    fake = FakeLLMClient([r1, r2])
    roles = discover(fake, search={"queries": ["fde"], "target_companies": ["Anthropic", "Cresta"], "boards": []})
    assert len(fake.calls) == 2                       # one web call per company
    assert all(c.web for c in fake.calls)
    assert sorted(r.company for r in roles) == ["Anthropic", "Cresta"]  # deduped across batches


def test_discover_partial_safe_when_one_company_fails():
    good = json.dumps([{"company": "Cresta", "title": "FDE", "url": "https://c/2", "location": "Remote AU"}])
    fake = FakeLLMClient(["not json at all", good])   # first company returns junk
    roles = discover(fake, search={"queries": ["fde"], "target_companies": ["BadCo", "Cresta"], "boards": []})
    assert [r.company for r in roles] == ["Cresta"]   # bad company skipped, good one kept
