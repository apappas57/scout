"""ATS discovery layer tests. Fully offline: every fetcher gets an injected
get_json fed from sanitised fixtures (real response shapes, invented companies)."""
import json
import tomllib
from pathlib import Path

import pytest

from scout import ats
from scout.ats import (
    Board,
    UNKNOWN_LOCATION,
    fetch_ats,
    fetch_ashby,
    fetch_greenhouse,
    fetch_lever,
    fetch_smartrecruiters,
    title_matches,
    verify_slug,
    _iso_date,
    _join_locations,
)

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "fixtures"

no_sleep = lambda s: None  # noqa: E731


def _fixture(name):
    return json.loads((FIX / name).read_text())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_title_matches_is_separator_insensitive():
    assert title_matches("Forward-Deployed Engineer", ats.DEFAULT_TITLE_KEYWORDS)
    assert title_matches("forward/deployed engineer", ats.DEFAULT_TITLE_KEYWORDS)
    assert not title_matches("Senior Accountant", ats.DEFAULT_TITLE_KEYWORDS)
    assert not title_matches("", ats.DEFAULT_TITLE_KEYWORDS)


def test_iso_date_handles_every_board_shape():
    assert _iso_date("2026-07-21T10:00:00-04:00") == "2026-07-21"
    assert _iso_date("2026-08-01T00:00:00Z") == "2026-08-01"
    assert _iso_date("2026-08-01") == "2026-08-01"
    assert _iso_date(1700000000000) == "2023-11-14"   # Lever epoch milliseconds
    assert _iso_date(None) == ""
    assert _iso_date("not a date") == ""


def test_join_locations_reports_absence_not_a_guess():
    assert _join_locations([]) == UNKNOWN_LOCATION
    assert _join_locations([None, ""]) == UNKNOWN_LOCATION
    assert _join_locations(["Melbourne", "melbourne", "Brisbane"]) == "Melbourne; Brisbane"


# ---------------------------------------------------------------------------
# Greenhouse
# ---------------------------------------------------------------------------


def _gh_get(calls):
    def get(url, *, timeout=None):
        calls.append(url)
        if url.endswith("/jobs/4400001002"):
            return 200, _fixture("ats_greenhouse_detail.json")
        assert url.endswith("/boards/exampleco/jobs")
        return 200, _fixture("ats_greenhouse.json")
    return get


def test_greenhouse_parses_matches_and_double_encoded_content():
    calls = []
    roles = fetch_greenhouse("ExampleCo", "exampleco", get_json=_gh_get(calls), sleep=no_sleep)
    assert [r.title for r in roles] == ["Forward Deployed Engineer", "Applied AI Engineer"]

    fde = roles[0]
    assert fde.source == "ats:greenhouse"
    assert fde.url == "https://boards.greenhouse.io/exampleco/jobs/4400001001"
    # location.name is authoritative and passed through verbatim
    assert fde.location == "London, UK; Melbourne, AUS; Remote-Friendly, Australia"
    assert fde.posted_at == "2026-07-21"                    # first_published wins over updated_at
    assert fde.snippet.startswith("Posted 2026-07-21. Workplace: Remote.")
    # double-encoded content: both unescape passes must have happened
    assert "Deploy & embed LLM systems" in fde.snippet
    assert "<" not in fde.snippet and "&lt;" not in fde.snippet and "&amp;" not in fde.snippet


def test_greenhouse_offices_fallback_and_detail_fetch():
    calls = []
    roles = fetch_greenhouse("ExampleCo", "exampleco", get_json=_gh_get(calls), sleep=no_sleep)
    ai = roles[1]
    assert ai.location == "Melbourne; Remote"               # offices fallback when location.name is null
    assert ai.posted_at == "2026-08-01"                     # updated_at fallback when first_published null
    assert "Own customer-facing prototypes" in ai.snippet   # body came from the detail endpoint
    # exactly one detail call, only for the posting with empty content
    assert [u for u in calls if u.endswith("/jobs/4400001002")] == [calls[1]]
    assert len(calls) == 2


def test_greenhouse_with_content_false_skips_detail_calls():
    calls = []
    roles = fetch_greenhouse(
        "ExampleCo", "exampleco", get_json=_gh_get(calls), sleep=no_sleep, with_content=False
    )
    assert len(calls) == 1                                  # list call only
    assert roles[1].snippet == "Posted 2026-08-01."


def test_greenhouse_dead_board_returns_empty():
    assert fetch_greenhouse("X", "gone", get_json=lambda url, *, timeout=None: (404, None), sleep=no_sleep) == []


# ---------------------------------------------------------------------------
# Lever
# ---------------------------------------------------------------------------


def test_lever_parses_locations_epoch_date_and_workplace():
    get = lambda url, *, timeout=None: (200, _fixture("ats_lever.json"))  # noqa: E731
    roles = fetch_lever("ExampleCo", "exampleco", get_json=get, sleep=no_sleep)
    assert [r.title for r in roles] == ["Solutions Engineer, APAC"]      # AE filtered out by title
    role = roles[0]
    assert role.source == "ats:lever"
    assert role.url == "https://jobs.lever.co/exampleco/a1b2c3d4-0000-4000-8000-000000000001"
    assert role.location == "Melbourne; Remote - Australia"              # allLocations preferred
    assert role.posted_at == "2023-11-14"                                # createdAt epoch ms
    assert "Workplace: remote." in role.snippet
    # descriptionPlain: entities unescaped, whitespace collapsed
    assert "customers to deploy the platform & prove value" in role.snippet


def test_lever_non_list_payload_returns_empty():
    get = lambda url, *, timeout=None: (200, {"error": "nope"})  # noqa: E731
    assert fetch_lever("X", "x", get_json=get, sleep=no_sleep) == []


# ---------------------------------------------------------------------------
# Ashby
# ---------------------------------------------------------------------------


def test_ashby_drops_unlisted_and_never_writes_remote_into_location():
    get = lambda url, *, timeout=None: (200, _fixture("ats_ashby.json"))  # noqa: E731
    roles = fetch_ashby("ExampleCo", "exampleco", get_json=get, sleep=no_sleep)
    assert [r.title for r in roles] == ["Deployed Engineer - APAC"]      # unlisted + coordinator gone
    role = roles[0]
    assert role.source == "ats:ashby"
    # location + secondaryLocations, case-insensitively deduplicated;
    # isRemote must NOT appear as a location (the London trap)
    assert role.location == "Melbourne; Brisbane"
    assert "remote" not in role.location.lower()
    # the disagreement between workplaceType and isRemote is surfaced as a label
    assert "Workplace: Hybrid, remote-flagged." in role.snippet
    assert role.posted_at == "2026-07-28"
    assert "Embed with customers" in role.snippet


# ---------------------------------------------------------------------------
# SmartRecruiters
# ---------------------------------------------------------------------------


def _sr_get(calls):
    def get(url, *, timeout=None):
        calls.append(url)
        if "/postings/744000059000001" in url:
            return 200, _fixture("ats_smartrecruiters_detail.json")
        if "/postings/744000059000002" in url:
            return 200, {"id": "744000059000002"}           # detail with no public URL
        assert "/postings?" in url
        return 200, _fixture("ats_smartrecruiters.json")
    return get


def test_smartrecruiters_detail_url_location_tidy_and_no_url_skip():
    calls = []
    roles = fetch_smartrecruiters("ExampleCo", "exampleco", get_json=_sr_get(calls), sleep=no_sleep)
    # Customer Engineer matched on title but its detail exposes no postingUrl -> dropped,
    # never emitted with a guessed link. Payroll Officer never matched.
    assert [r.title for r in roles] == ["Implementation Engineer"]
    role = roles[0]
    assert role.source == "ats:smartrecruiters"
    assert role.url == "https://jobs.smartrecruiters.com/ExampleCo/744000059000001-implementation-engineer"
    assert role.location == "Beijing, China"                # empty middle segment tidied
    assert role.posted_at == "2026-08-01"
    assert "Workplace: hybrid." in role.snippet
    # jobDescription + qualifications in, companyDescription out
    assert "Guide customers through rollout & go-live." in role.snippet
    assert "5+ years in delivery" in role.snippet
    assert "Boilerplate" not in role.snippet


def test_smartrecruiters_pages_until_total_found():
    p1 = {"id": "1", "name": "Gardener", "location": {"fullLocation": "Warsaw, Poland"}}
    p2 = {"id": "2", "name": "Florist", "location": {"fullLocation": "Warsaw, Poland"}}
    calls = []

    def get(url, *, timeout=None):
        calls.append(url)
        if "offset=0" in url:
            return 200, {"totalFound": 2, "content": [p1]}
        return 200, {"totalFound": 2, "content": [p2]}

    fetch_smartrecruiters("X", "x", get_json=get, sleep=no_sleep)
    assert "offset=0" in calls[0] and "offset=1" in calls[1] and len(calls) == 2


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_board_from_mapping_accepts_both_key_spellings():
    a = Board.from_mapping({"name": "ExampleCo", "board": "GREENHOUSE", "slug": "exampleco"})
    b = Board.from_mapping({"company": "ExampleCo", "board": "greenhouse", "slug": "exampleco"})
    assert a == b == Board(company="ExampleCo", board="greenhouse", slug="exampleco")


@pytest.mark.parametrize("row", [
    {"name": "X", "board": "greenhouse"},                   # missing slug
    {"board": "lever", "slug": "x"},                        # missing company/name
    {"name": "X", "board": "workable", "slug": "x"},        # unsupported board
])
def test_board_from_mapping_rejects_bad_rows(row):
    with pytest.raises(ValueError):
        Board.from_mapping(row)


def test_shipped_registry_round_trips_through_board():
    """Every row in the real shipped registry must coerce into a fetchable Board."""
    with open(ROOT / "config" / "ats_companies.toml", "rb") as fh:
        rows = tomllib.load(fh).get("company") or []
    assert rows, "shipped registry is empty"
    boards = [Board.from_mapping(row) for row in rows]
    assert all(b.board in ats.FETCHERS for b in boards)
    assert all(b.company and b.slug for b in boards)


def test_fetch_ats_merges_dedupes_and_survives_bad_entries():
    def get(url, *, timeout=None):
        if "boards/boom/" in url:
            raise RuntimeError("kaboom")
        if "greenhouse" in url:
            if url.endswith("/jobs/4400001002"):
                return 200, _fixture("ats_greenhouse_detail.json")
            return 200, _fixture("ats_greenhouse.json")
        assert "lever" in url
        return 200, _fixture("ats_lever.json")

    registry = [
        Board(company="ExampleCo", board="greenhouse", slug="exampleco"),
        {"name": "ExampleCo Lever", "board": "lever", "slug": "exampleco"},
        {"company": "ExampleCo", "board": "greenhouse", "slug": "exampleco"},  # dup -> deduped by role id
        {"name": "Broken", "board": "workable", "slug": "x"},                  # bad row -> skipped
        {"name": "BoomCo", "board": "greenhouse", "slug": "boom"},             # fetcher raises -> skipped
    ]
    roles = fetch_ats(registry, get_json=get, sleep=no_sleep)
    assert len(roles) == 3                                  # 2 greenhouse (deduped) + 1 lever
    assert {r.source for r in roles} == {"ats:greenhouse", "ats:lever"}
    assert len({r.id for r in roles}) == 3


# ---------------------------------------------------------------------------
# verify_slug
# ---------------------------------------------------------------------------


def test_verify_slug_ok_zero_and_error_cases():
    ok, count, note = verify_slug(
        "greenhouse", "exampleco",
        get_json=lambda url, *, timeout=None: (200, _fixture("ats_greenhouse.json")),
    )
    assert (ok, count, note) == (True, 3, "ok")

    ok, count, note = verify_slug(
        "lever", "empty", get_json=lambda url, *, timeout=None: (200, [])
    )
    assert not ok and note == "200 but zero postings"       # empty 200 is NOT a good slug

    ok, _, note = verify_slug(
        "ashby", "gone", get_json=lambda url, *, timeout=None: (404, None)
    )
    assert not ok and note == "HTTP 404"

    ok, _, note = verify_slug("workable", "x", get_json=lambda url, *, timeout=None: (200, {}))
    assert not ok and "unsupported board" in note
