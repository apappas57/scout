"""Board #296 regressions: the remote gate must score the board's own arrangement
field, not just the location string, and must read the FULL posting body.

Two real misclassifications drove this, both verified against the live boards on
12 Aug 2026 and both sitting in scout.db as HIGH/MED rows:

    Lyrebird Health, Forward Deployed Engineer (jobs.lever.co/lyrebirdhealth/...):
        location "Melbourne" (allowed), Lever workplaceType "onsite". The gate
        scored the location string, so an onsite role passed a remote-first hard
        requirement.
    Talkdesk, Senior Applied AI Partner Solution Engineer - APAC (greenhouse
        7862517): location "Remote", body "Ability to travel approximately 35-70%
        of the time". The travel clause sat past the 600-char snippet cut, so the
        signal never reached the gate.

Fixture strings below are quoted from the live postings and from the persisted
scout.db rows, trimmed but not paraphrased.
"""
from scout import track
from scout.filter_dedupe import _location_ok
from scout.models import Role
from scout.signals import assess_remote, remote_gate

FILTERS = {
    "remote_only": True,
    "allow_locations": ["Melbourne", "Australia", "Remote", "AU Remote", "APAC Remote"],
    "exclude_locations": ["Sydney", "United States", "San Francisco", "New York", "Tel Aviv"],
}

# The persisted snippet lead for the Lyrebird row, exactly as ats._compose_snippet
# wrote it into scout.db before the workplace column existed.
LYREBIRD_SNIPPET = (
    "Posted 2026-05-15. Workplace: onsite. The Role Lyrebird is expanding its "
    "enterprise footprint across healthcare systems in Australia, the UK, and the "
    "UAE, and this role sits at the sharp end of that growth."
)

LYREBIRD_BODY = (
    "The Role Lyrebird is expanding its enterprise footprint across healthcare "
    "systems in Australia, the UK, and the UAE, and this role sits at the sharp "
    "end of that growth. As a Forward Deployed Engineer, you'll be the technical "
    "bridge between our product and the complex environments our largest "
    "customers operate in."
)

# The clause the 600-char snippet cut off, quoted from the live Greenhouse body.
TALKDESK_TRAVEL = (
    "Market Representation: Represent Talkdesk as a technical expert at regional "
    "Partner summits, technical events, webinars, and conferences. Travel: Ability "
    "to travel approximately 35-70% of the time across Australia, New Zealand, "
    "Singapore, and other ASEAN countries as required."
)


def _lyrebird(**overrides):
    fields = dict(
        company="Lyrebird Health",
        title="Forward Deployed Engineer",
        url="https://jobs.lever.co/lyrebirdhealth/a532551a-c589-460c-9875-ad2c6d1c3948",
        location="Melbourne",
        source="ats:lever",
        snippet=LYREBIRD_SNIPPET,
        workplace="onsite",
        description=LYREBIRD_BODY,
    )
    fields.update(overrides)
    return Role(**fields)


def _talkdesk():
    return Role(
        company="Talkdesk",
        title="Senior Applied AI Partner Solution Engineer - APAC",
        url="https://job-boards.greenhouse.io/talkdesk2/jobs/7862517",
        location="Remote",
        source="ats:greenhouse",
        description=TALKDESK_TRAVEL,
    )


# ---------------------------------------------------------------------------
# Lyrebird: the arrangement field outranks an allowed location string
# ---------------------------------------------------------------------------


def test_lyrebird_location_string_alone_passes_the_old_gate():
    # The misclassification being locked out: substring matching on the location
    # string has no objection to an onsite Melbourne role.
    assert _location_ok("Melbourne", FILTERS)


def test_lyrebird_onsite_workplace_field_blocks_despite_allowed_location():
    verdict = assess_remote(_lyrebird(), filters=FILTERS, description=LYREBIRD_BODY)
    assert not verdict.ok and verdict.verdict == "BLOCK"
    assert "onsite" in verdict.reason


def test_lyrebird_db_row_shape_blocks_via_snippet_fallback():
    # Rows persisted before the workplace column existed carry the arrangement
    # only in the snippet lead. The fix must reach those rows too.
    role = _lyrebird(workplace="", description="")
    verdict = assess_remote(role, filters=FILTERS)
    assert not verdict.ok and verdict.verdict == "BLOCK"
    assert "onsite" in verdict.reason


# ---------------------------------------------------------------------------
# Talkdesk: the travel clause lives past the snippet cut
# ---------------------------------------------------------------------------


def test_talkdesk_heavy_travel_in_full_body_blocks():
    role = _talkdesk()
    verdict = assess_remote(role, filters=FILTERS, description=role.description)
    assert not verdict.ok and verdict.verdict == "BLOCK"
    assert "travel" in verdict.reason and "70%" in verdict.reason


def test_talkdesk_truncated_snippet_alone_would_have_passed():
    # Why full-body persistence matters: with only the persisted 600-char snippet
    # (which ends before the travel clause), the gate has nothing to object to.
    role = _talkdesk()
    verdict = assess_remote(
        role,
        filters=FILTERS,
        description="Posted 2026-07-20. The Sr. Partner Solutions Engineer is a "
                    "highly experienced technical lead responsible for driving the "
                    "growth and technical enablement of our key partners.",
    )
    assert verdict.ok


# ---------------------------------------------------------------------------
# Guard rails: the new rules must not over-block
# ---------------------------------------------------------------------------


def test_remote_workplace_field_is_positive_evidence_not_a_block():
    role = _lyrebird(workplace="Remote", location="Remote - Australia",
                     snippet="", description="")
    verdict = assess_remote(role, filters=FILTERS,
                            description="Deploy with enterprise customers across Australia.")
    assert verdict.ok and verdict.verdict == "PASS"


def test_contradictory_ashby_label_is_a_caveat_not_a_block():
    # Ashby's "Hybrid, remote-flagged" is the board disagreeing with itself.
    role = _lyrebird(workplace="Hybrid, remote-flagged", location="Remote - Australia",
                     snippet="", description="")
    verdict = assess_remote(role, filters=FILTERS,
                            description="Work with customers across Australia.")
    assert verdict.ok
    assert any("disagrees with itself" in e for e in verdict.evidence)


def test_light_travel_is_a_caveat_not_a_block():
    role = _lyrebird(workplace="Remote", location="Remote - Australia",
                     snippet="", description="")
    verdict = assess_remote(
        role, filters=FILTERS,
        description="Fully remote across Australia with occasional travel up to 10% for kickoffs.",
    )
    assert verdict.ok
    assert any("10%" in c for c in verdict.evidence)


def test_percentage_before_a_comma_break_is_not_a_travel_load():
    role = _lyrebird(workplace="Remote", location="Remote - Australia",
                     snippet="", description="")
    verdict = assess_remote(
        role, filters=FILTERS,
        description="This role is 100% remote across Australia, and travel is never required.",
    )
    assert verdict.ok and verdict.verdict == "PASS"


# ---------------------------------------------------------------------------
# End to end: gate plus persistence
# ---------------------------------------------------------------------------


def test_remote_gate_rejects_both_named_regressions_and_keeps_a_clean_role():
    good = Role(company="ExampleCo", title="Solutions Engineer",
                url="https://example.invalid/j/1", location="Remote - Australia",
                workplace="Remote", description="Work across APAC with enterprise customers.")
    roles = [_lyrebird(), _talkdesk(), good]
    kept, rejected = remote_gate(
        roles, filters=FILTERS, descriptions={r.id: r.description for r in roles},
    )
    assert kept == [good]
    assert {r.company for r, _ in rejected} == {"Lyrebird Health", "Talkdesk"}
    assert all(v.verdict == "BLOCK" for _, v in rejected)


def test_workplace_and_full_description_survive_the_db_roundtrip(conn):
    # The signal only exists on the next run if it is persisted whole: a 600-char
    # snippet is what hid Talkdesk's travel clause in the first place.
    long_body = TALKDESK_TRAVEL + " " + ("Partner enablement across the region. " * 30)
    assert len(long_body) > 600
    role = _talkdesk()
    role.description = long_body
    role.workplace = "Remote"
    track.upsert_roles(conn, [role], now="2026-08-12T00:00:00+00:00")
    row = conn.execute("SELECT * FROM roles WHERE id = ?", (role.id,)).fetchone()
    assert row["workplace"] == "Remote"
    assert row["description"] == long_body
    loaded = track._row_to_role(row)
    assert loaded.description == long_body and loaded.workplace == "Remote"


def test_bodyless_resight_does_not_erase_persisted_signal(conn):
    # A later run can legitimately re-sight the same role without a body: Greenhouse
    # roles past the ats max_detail cap, or a with_content=false run, arrive with
    # workplace="" and description="". The upsert must keep the stored signal, or the
    # Talkdesk travel clause would be deleted on re-sight and the role would drift
    # from BLOCK back to REVIEW.
    role = _talkdesk()
    role.workplace = "Remote"
    track.upsert_roles(conn, [role], now="2026-08-12T00:00:00+00:00")
    resight = _talkdesk()
    resight.workplace = ""
    resight.description = ""
    assert resight.id == role.id
    track.upsert_roles(conn, [resight], now="2026-08-13T00:00:00+00:00")
    row = conn.execute("SELECT * FROM roles WHERE id = ?", (role.id,)).fetchone()
    assert row["workplace"] == "Remote"
    assert row["description"] == TALKDESK_TRAVEL
    assert row["last_seen_at"] == "2026-08-13T00:00:00+00:00"
