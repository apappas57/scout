from scout.models import Role, Score, canonical_url, role_id


def test_role_id_is_stable_and_url_canonical():
    a = Role(company="Acme", title="FDE", url="https://x.com/jobs/1?utm_source=foo", location="Remote AU")
    b = Role(company="acme", title="  FDE ", url="https://x.com/jobs/1", location="Remote AU")
    assert canonical_url("https://x.com/jobs/1?utm_source=foo#x") == "https://x.com/jobs/1"
    assert role_id(a) == role_id(b)  # case/whitespace/tracking-param insensitive


def test_score_enum_ordering():
    assert Score.HIGH.rank > Score.MED.rank > Score.LOW.rank
