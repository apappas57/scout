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
