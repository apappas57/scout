"""Signals stage tests: the hard remote-eligibility gate and the low-contest proxy.
Everything runs offline; the aggregator probe is either a stub or absent."""
from scout.filter_dedupe import _location_ok
from scout.models import Role
from scout.signals import (
    AggregatorProbe,
    BAND_CROWDED,
    BAND_QUIET,
    BAND_UNMEASURED,
    assess_remote,
    contest_signal,
    rank_by_contest,
    remote_gate,
    _aggregator_value,
    _freshness_value,
)

FILTERS = {
    "remote_only": True,
    "allow_locations": ["Melbourne", "Australia", "Remote", "AU Remote", "APAC Remote"],
    "exclude_locations": ["Sydney", "United States", "San Francisco", "New York", "Tel Aviv"],
}


def _role(title="Forward Deployed Engineer", company="ExampleCo",
          location="Remote - Australia", snippet=""):
    return Role(company=company, title=title, url="https://example.invalid/j/1",
                location=location, source="ats:test", snippet=snippet)


# ---------------------------------------------------------------------------
# Remote gate: assess_remote
# ---------------------------------------------------------------------------


def test_pass_when_allowed_region_in_location_and_clean_body():
    verdict = assess_remote(
        _role(location="Remote - Australia"),
        filters=FILTERS,
        description="Work with customers across APAC to deploy agents.",
    )
    assert verdict.ok and verdict.verdict == "PASS" and verdict.confidence == "high"


def test_block_us_qualified_remote_that_substring_filter_passes():
    # The exact failure mode this module exists for: "Remote" satisfies the
    # substring allow-list while ", DC" marks it US-only.
    location = "Remote-Friendly (Travel-Required) | Washington, DC"
    assert _location_ok(location, FILTERS)                  # the old gate lets it through
    verdict = assess_remote(_role(location=location), filters=FILTERS,
                            description="Ship prototypes with customers.")
    assert not verdict.ok and verdict.verdict == "BLOCK" and verdict.confidence == "high"
    assert "United States" in verdict.reason


def test_block_residency_clause_hidden_in_body():
    verdict = assess_remote(
        _role(location="Remote"),
        filters=FILTERS,
        description="Candidates must be authorized to work in the United States.",
    )
    assert not verdict.ok and verdict.verdict == "BLOCK"
    assert "United States" in verdict.reason


def test_block_remote_qualified_to_unrecognised_place():
    # "Brussels" is in no exclude list; the gate must still refuse to treat an
    # unrecognised geographic qualifier as AU-eligible.
    verdict = assess_remote(_role(location="Remote - Brussels"), filters=FILTERS,
                            description="Deploy the platform with customers.")
    assert not verdict.ok and verdict.verdict == "BLOCK"
    assert any("unrecognised place" in e for e in verdict.evidence)


def test_sydney_melbourne_reads_as_choice_of_office_not_a_block():
    verdict = assess_remote(_role(location="Sydney, Melbourne"), filters=FILTERS,
                            description="Deploy with customers in Australia.")
    assert verdict.ok and verdict.verdict == "PASS"
    assert any("choice of office" in e for e in verdict.evidence)


def test_review_when_no_description_and_no_allowed_region():
    verdict = assess_remote(_role(location="Remote", snippet=""), filters=FILTERS,
                            description="")
    assert verdict.ok and verdict.verdict == "REVIEW" and verdict.confidence == "low"
    assert verdict.cannot_catch                            # limitations travel with the verdict


def test_block_foreign_timezone_stated_as_requirement():
    verdict = assess_remote(
        _role(location="Remote"),
        filters=FILTERS,
        description="You must maintain at least four hours of overlap with PST business hours.",
    )
    assert not verdict.ok and verdict.verdict == "BLOCK"
    assert "timezone" in verdict.reason


# ---------------------------------------------------------------------------
# Remote gate: remote_gate
# ---------------------------------------------------------------------------


def test_remote_gate_splits_annotates_and_respects_allow_review():
    good = _role(title="Solutions Engineer", location="Remote - Australia")
    bad = _role(title="Applied AI Engineer", location="Remote - United States")
    quiet = _role(title="Customer Engineer", location="Remote")  # no body -> REVIEW

    kept, rejected = remote_gate(
        [good, bad, quiet],
        filters=FILTERS,
        descriptions={good.id: "Work across APAC with enterprise customers."},
    )
    assert good in kept and quiet in kept                  # REVIEW kept by default
    assert [r for r, _ in rejected] == [bad]
    assert rejected[0][1].verdict == "BLOCK"
    assert all(hasattr(r, "remote_verdict") for r in (good, bad, quiet))

    strict_kept, strict_rejected = remote_gate([good, quiet], filters=FILTERS,
                                               descriptions={good.id: "APAC customers."},
                                               allow_review=False)
    assert strict_kept == [good]                           # strict pass drops REVIEW
    assert [r for r, _ in strict_rejected] == [quiet]


# ---------------------------------------------------------------------------
# Low-contest proxy
# ---------------------------------------------------------------------------


def test_contest_quiet_role_scores_high_with_full_measurement():
    role = _role(title="Forward Deployed Engineer", company="Obscure Robotics",
                 location="Melbourne, Australia")
    signal = contest_signal(
        role, age_days=1, board_size=10,
        aggregator=AggregatorProbe(hits=0, domains=[], query="q"),
    )
    assert signal.proxy_score >= 70 and signal.band == BAND_QUIET
    assert signal.measured_weight == 100
    assert signal.is_proxy
    # the proxy must always say it is a proxy, never an applicant count
    assert any("proxy" in c for c in signal.caveats)
    assert "proxy" in signal.explain()


def test_contest_crowded_famous_generic_worldwide_role():
    role = _role(title="Software Engineer", company="Anthropic",
                 location="Remote (Worldwide)")
    signal = contest_signal(
        role, age_days=20, board_size=400,
        aggregator=AggregatorProbe(hits=3, domains=["linkedin.com", "indeed.com", "seek.com.au"], query="q"),
    )
    assert signal.proxy_score < 45 and signal.band == BAND_CROWDED
    assert any("household name" in c.detail for c in signal.components)


def test_contest_unmeasurable_role_yields_no_score():
    signal = contest_signal(Role(company="", title="", url="https://x", location=""))
    assert signal.proxy_score is None and signal.band == BAND_UNMEASURED
    assert signal.measured_weight == 0.0


def test_missing_aggregator_probe_is_unmeasured_not_zero():
    signal = contest_signal(_role(), age_days=3, board_size=50, aggregator=None)
    agg = next(c for c in signal.components if c.name == "aggregator_absence")
    assert agg.value is None and agg.points == 0.0 and not agg.measured
    assert signal.measured_weight == 70                    # 100 minus the 30-point probe
    assert any("strongest signal" in c for c in signal.caveats)


def test_aggregator_value_ladder():
    assert _aggregator_value(None) == (None, "aggregator probe unavailable, component excluded from the score")
    assert _aggregator_value(AggregatorProbe(0, [], "q"))[0] == 1.0
    assert _aggregator_value(AggregatorProbe(1, ["seek.com.au"], "q"))[0] == 0.55
    assert _aggregator_value(AggregatorProbe(5, ["a", "b", "c", "d", "e"], "q"))[0] == 0.10


def test_freshness_is_u_shaped_not_linear():
    day0, _ = _freshness_value(0)
    day20, _ = _freshness_value(20)
    day40, _ = _freshness_value(40)
    day60, _ = _freshness_value(60)
    assert day0 > day20 > day40                            # front-loaded decay
    assert day60 > day40                                   # stalled-funnel recovery...
    assert day60 < day0                                    # ...capped well below fresh
    assert _freshness_value(None) == (None, "posting date unknown")


def test_rank_by_contest_orders_quiet_first_and_survives_probe_errors():
    quiet = _role(title="Forward Deployed Engineer", company="Obscure Robotics",
                  location="Melbourne, Australia")
    crowded = _role(title="Software Engineer", company="Anthropic",
                    location="Remote (Worldwide)")
    unmeasured = Role(company="", title="", url="https://y", location="")

    def boom(role):
        raise RuntimeError("probe down")

    ranked = rank_by_contest(
        [crowded, quiet, unmeasured],
        probe=boom,                                        # a dying probe must not lose the batch
        board_sizes={"obscure robotics": 10, "anthropic": 400},
        ages={quiet.id: 1.0, crowded.id: 20.0},
    )
    assert [r.title for r, _ in ranked] == ["Forward Deployed Engineer", "Software Engineer", ""]
    assert ranked[-1][1].proxy_score is None               # unmeasured sorts last
    assert all(hasattr(r, "contest") for r, _ in ranked)
