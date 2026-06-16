from scout.models import Role
from scout.verify import verify

FILTERS = {"remote_only": True, "allow_locations": ["Remote"], "exclude_locations": ["Sydney"], "exclude_keywords": []}


def test_verify_drops_dead_and_closed_and_keeps_live():
    pages = {
        "https://live/1": (200, "Apply now. Remote. Great role."),
        "https://dead/2": (404, "Not Found"),
        "https://closed/3": (200, "We are no longer accepting applications for this role."),
    }
    roles = [Role("A", "FDE", u, "Remote") for u in pages]
    out = verify(roles, fetch=lambda u: pages[u], filters=FILTERS)
    assert [r.url for r in out] == ["https://live/1"]
