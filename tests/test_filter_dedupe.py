from scout.models import Role
from scout.filter_dedupe import filter_roles

FILTERS = {
    "remote_only": True,
    "allow_locations": ["Melbourne", "Remote", "Australia"],
    "exclude_locations": ["Sydney", "United States", "Tel Aviv"],
    "exclude_keywords": ["internship", "unpaid"],
}


def test_drops_seen_and_excluded_location_and_keyword_and_dupes():
    roles = [
        Role("Acme", "FDE", "https://a.com/1", "Remote AU"),          # keep
        Role("Acme", "FDE", "https://a.com/1?utm_source=x", "Remote"),# dupe of above -> drop
        Role("Beta", "AI Eng", "https://b.com/2", "Sydney"),          # excluded location
        Role("Gamma", "FDE Internship", "https://c.com/3", "Remote"), # excluded keyword
        Role("Delta", "FDE", "https://d.com/4", "Tel Aviv"),          # excluded location
    ]
    seen = {Role("Old", "X", "https://old.com/9", "Remote").id}
    out = filter_roles(roles, seen_ids=seen, filters=FILTERS)
    assert [r.company for r in out] == ["Acme"]


def test_allow_list_blocks_unknown_location_when_remote_only():
    roles = [Role("Z", "FDE", "https://z.com/1", "Berlin office only")]
    out = filter_roles(roles, seen_ids=set(), filters=FILTERS)
    assert out == []
