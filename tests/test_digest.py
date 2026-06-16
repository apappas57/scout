from scout.models import Role, Score, RunResult
from scout.digest import compose_digest


def test_digest_lists_high_with_reasons_and_no_em_dash():
    roles = [Role("Anthropic", "FDE", "u", "Remote", score=Score.HIGH, score_reason="target company")]
    text = compose_digest(shortlist=roles, med=[], stale=[], run=RunResult("r1", high=1, drafted=1))
    assert "Anthropic" in text and "target company" in text
    assert "—" not in text   # no em dash
