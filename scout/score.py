from __future__ import annotations
from scout.models import Role, Score


def _any_hit(haystack: str, needles: list[str]) -> list[str]:
    """Return the needles (lowercased) that appear in the haystack."""
    hay = haystack.lower()
    return [n for n in needles if n.lower() in hay]


def score_role(role: Role, *, scoring: dict) -> Role:
    """Assign HIGH/MED/LOW deterministically and record a one-line reason.

    HIGH when the company is a target company, OR when at least two of these
    signals fire: the title matches a high keyword, the title matches a
    seniority keyword, the snippet matches a profile keyword. Exactly one
    signal is MED. No signals is LOW."""
    high_companies = scoring.get("high_companies", [])
    high_keywords = scoring.get("high_keywords", [])
    seniority_keywords = scoring.get("seniority_keywords", [])
    profile_keywords = scoring.get("profile_keywords", high_keywords)

    is_target = any(c.lower() == role.company.strip().lower() for c in high_companies)

    title_kw = _any_hit(role.title, high_keywords)
    title_sen = _any_hit(role.title, seniority_keywords)
    snippet_kw = _any_hit(role.snippet, profile_keywords) if role.snippet else []

    reasons: list[str] = []
    signals = 0
    if title_kw:
        signals += 1
        reasons.append("role-type match: " + ", ".join(title_kw))
    if title_sen:
        signals += 1
        reasons.append("seniority match: " + ", ".join(title_sen))
    if snippet_kw:
        signals += 1
        reasons.append("profile keyword in snippet: " + ", ".join(snippet_kw))

    if is_target:
        role.score = Score.HIGH
        target_reason = f"target company ({role.company})"
        role.score_reason = "; ".join([target_reason] + reasons) if reasons else target_reason
    elif signals >= 2:
        role.score = Score.HIGH
        role.score_reason = "; ".join(reasons)
    elif signals == 1:
        role.score = Score.MED
        role.score_reason = "; ".join(reasons)
    else:
        role.score = Score.LOW
        role.score_reason = "no target signals fired"

    return role
