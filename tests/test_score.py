from scout.models import Role, Score
from scout.score import score_role

SCORING = {
    "high_companies": ["Anthropic"],
    "high_keywords": ["forward deployed", "applied ai"],
    "seniority_keywords": ["senior", "staff"],
}


def test_target_company_is_high():
    r = score_role(Role("Anthropic", "Engineer", "u", "Remote"), scoring=SCORING)
    assert r.score is Score.HIGH and "target company" in r.score_reason.lower()


def test_two_signals_is_high_one_is_med():
    high = score_role(Role("X", "Senior Forward Deployed Engineer", "u", "Remote"), scoring=SCORING)
    med = score_role(Role("Y", "Applied AI Engineer", "u", "Remote"), scoring=SCORING)
    low = score_role(Role("Z", "Marketing Manager", "u", "Remote"), scoring=SCORING)
    assert high.score is Score.HIGH
    assert med.score is Score.MED
    assert low.score is Score.LOW
