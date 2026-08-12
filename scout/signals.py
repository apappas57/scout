"""Signals stage: a hard remote-eligibility gate and a transparent low-contest proxy.

Two problems live here, and they are separate on purpose.

**1. The remote gate.** ``filter_dedupe._location_ok`` does substring matching over
the allow and exclude lists. That is fast and right most of the time, but it passes
the exact case Alex loses days to: a posting whose location field reads "Remote" (or
"Remote-Friendly (Travel-Required) | Washington, DC", a real Anthropic posting that
passes the current filter today) and whose body says the role is US-only. This module
reads the text around every remote mention, plus residency, work-authorisation,
onsite and timezone clauses, and returns a verdict with the exact sentence that
caused it. It is deliberately conservative: it BLOCKs only on high-confidence
evidence and otherwise returns REVIEW, because a false BLOCK silently deletes a good
role and Alex never sees it.

**2. The low-contest proxy.** Alex wants roles with low application volume. No ATS
board publishes an applicant count, so it cannot be measured. This module does NOT
invent one. It computes a weighted 0-100 *proxy* for how under-contested a role is
likely to be, exposes every component with its value and its reasoning, and labels
the result a proxy everywhere it surfaces. Nothing in this module ever emits a number
of applicants. If Alex disagrees with a ranking he can read the components and see
precisely which signal he disagrees with.

Both entry points are pure given their inputs. The only network call is the optional
aggregator probe, which is best-effort and returns None on any failure, so a dead
search endpoint degrades the proxy rather than aborting a run.
"""
from __future__ import annotations

import html
import re
import time
import urllib.parse
from dataclasses import dataclass, field

from scout.filter_dedupe import _location_ok
from scout.models import Role

# Politeness settings for the one optional network call in this module.
USER_AGENT = "scout-signals/0.1 (+personal job-hunt agent; contact apappas57@gmail.com)"
TIMEOUT_SECONDS = 15
POLITE_DELAY_SECONDS = 1.5

_last_network_call = 0.0


# --------------------------------------------------------------------------
# Region vocabulary
# --------------------------------------------------------------------------
# Two-letter US state codes are only matched when comma-preceded ("San Francisco, CA"),
# which is how every ATS writes them. Matching them bare would fire on "IN", "OR",
# "ME" and "HI" in ordinary prose.
_US_STATE_CODES = (
    "AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|"
    "MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|DC"
)
_US_STATE_NAMES = (
    "alabama|alaska|arizona|arkansas|california|colorado|connecticut|delaware|florida|"
    "georgia|hawaii|idaho|illinois|indiana|iowa|kansas|kentucky|louisiana|maine|maryland|"
    "massachusetts|michigan|minnesota|mississippi|missouri|montana|nebraska|nevada|"
    "new hampshire|new jersey|new mexico|new york|north carolina|north dakota|ohio|"
    "oklahoma|oregon|pennsylvania|rhode island|south carolina|south dakota|tennessee|"
    "texas|utah|vermont|virginia|washington|west virginia|wisconsin|wyoming"
)

# Each entry is (label, pattern). Acronyms are wrapped in (?-i: ) so they stay
# case-sensitive inside an otherwise case-insensitive compile: "US" is a country,
# "us" is a pronoun and appears in almost every job ad ("come build with us").
_EXCLUDED_REGIONS: tuple[tuple[str, str], ...] = (
    ("United States", rf"united states(?: of america)?|u\.s\.a?\.|stateside|conus|"
                      rf"(?-i:\bUSA?\b)|,\s*(?-i:(?:{_US_STATE_CODES}))\b|\b(?:{_US_STATE_NAMES})\b"),
    ("Canada", r"\bcanada\b|\bcanadian\b|\bontario\b|\btoronto\b|\bvancouver\b|\bmontreal\b|(?-i:\bCAN\b)"),
    ("United Kingdom", r"united kingdom|\bengland\b|\bscotland\b|\blondon\b|\bmanchester\b|(?-i:\bUK\b)"),
    ("Ireland", r"\bireland\b|\bdublin\b|(?-i:\bIE\b)"),
    ("Europe", r"\beurope\b|\beuropean\b|(?-i:\bEMEA\b)|\bgermany\b|\bberlin\b|\bmunich\b|"
               r"\bfrance\b|\bparis\b|\bspain\b|\bmadrid\b|\bnetherlands\b|\bamsterdam\b|"
               r"\bpoland\b|\bswitzerland\b|\bzurich\b|\bz.rich\b|\bsweden\b|\bportugal\b|\blisbon\b"),
    ("Singapore", r"\bsingapore\b"),
    ("India", r"\bindia\b|\bbangalore\b|\bbengaluru\b|\bhyderabad\b|\bmumbai\b|\bpune\b|\bgurgaon\b"),
    ("Latin America", r"latin america|(?-i:\bLATAM\b)|\bbrazil\b|\bmexico\b|\bargentina\b|\bcolombia\b"),
    ("Japan", r"\bjapan\b|\btokyo\b"),
    ("Korea", r"south korea|\bseoul\b"),
    ("China", r"\bchina\b|\bbeijing\b|\bshanghai\b|\bshenzhen\b|hong kong"),
    ("Israel", r"\bisrael\b|tel aviv"),
    ("Americas", r"\bamericas\b|north america"),
    ("Africa", r"south africa\b|\bjohannesburg\b|\bcape town\b|\bnairobi\b|\bkenya\b|\bnigeria\b|\blagos\b"),
    # Sydney is excluded for Alex specifically: it is a domestic relocation he ruled out.
    ("Sydney", r"\bsydney\b"),
)

# Locations Alex can actually take. Matching one of these near a remote mention is
# what rescues a posting from the qualified-remote rule.
_ALLOWED_REGIONS: tuple[tuple[str, str], ...] = (
    ("Australia", r"\baustralia\b|\baustralian\b|\bmelbourne\b|\bvictoria\b|\bbrisbane\b|"
                  r"\bperth\b|\badelaide\b|\bcanberra\b|(?-i:\bAU\b|\bANZ\b|\bAEST\b|\bAEDT\b|\bAET\b)"),
    ("APAC", r"\bapac\b|asia[- ]pacific|asia pac\b"),
    ("Worldwide", r"\bworldwide\b|\bglobally\b|\bglobal\b|\banywhere\b|any country|"
                  r"any (?:time ?zone|timezone)|fully distributed"),
)

# City-level places Alex can work. Distinct from _ALLOWED_REGIONS because only a
# city can prove a multi-site posting: "Sydney, Melbourne" is a choice of two offices,
# "Sydney, Australia" is one office and its country.
_ALLOWED_CITY = re.compile(
    r"\bmelbourne\b|\bbrisbane\b|\bperth\b|\badelaide\b|\bcanberra\b|\bhobart\b", re.IGNORECASE
)

_REMOTE_TOKEN = r"(?:remote(?:[- ]?friendly|[- ]?first|[- ]?only)?|work from home|\bwfh\b|fully distributed)"

# The board's structured arrangement field (Lever/Ashby workplaceType, Greenhouse
# "Location Type", SmartRecruiters remote/hybrid flags). ats._make_role persists it
# on Role.workplace and also writes it into the snippet lead ("Workplace: onsite."),
# which is the only place it survived for rows stored before the field existed.
_WORKPLACE_IN_SNIPPET = re.compile(r"\bWorkplace:\s*([^.]{1,60})\.", re.IGNORECASE)
_WP_ONSITE = re.compile(r"\bon[- ]?site\b|\bin[- ]?(?:office|person)\b", re.IGNORECASE)
_WP_HYBRID = re.compile(r"\bhybrid\b", re.IGNORECASE)
_WP_REMOTE = re.compile(r"\bremote\b|\bwfh\b|work from home|\bdistributed\b", re.IGNORECASE)

# Travel commitments stated as a share of working time. Quarterly travel is fine
# under the remote-first requirement; a stated load at or above this share means
# the role is a field role wearing a "Remote" location string (Talkdesk's APAC
# Solution Engineer: location "Remote", body "travel approximately 35-70%").
_TRAVEL_BLOCK_PCT = 25
_TRAVEL_RANGE = r"(\d{1,3})\s*%?\s*(?:[-–]|to)\s*(\d{1,3})\s*%|(\d{1,3})\s*%"
# Commas are excluded from the connector on both sides so "100% remote, some
# travel" never reads as a 100% travel load: the clause has to be one breath.
_TRAVEL_NEAR = re.compile(
    rf"\btravel\w*[^.;,%]{{0,40}}?(?:{_TRAVEL_RANGE})|(?:{_TRAVEL_RANGE})[^.;,%]{{0,40}}?\btravel",
    re.IGNORECASE,
)


def _workplace_label(role: Role) -> str:
    """The board's own arrangement claim for this role, or "".

    Role.workplace is authoritative. The snippet lead is the fallback for roles
    persisted before the workplace column existed, so the fix reaches the rows
    already sitting in scout.db.
    """
    label = (getattr(role, "workplace", "") or "").strip()
    if label:
        return label
    m = _WORKPLACE_IN_SNIPPET.search(getattr(role, "snippet", "") or "")
    return m.group(1).strip() if m else ""

# The bracketed or dash-delimited qualifier that follows "Remote" in a location field:
# "Remote (South Africa)", "Remote - Brussels", "Remote-Friendly (Travel-Required)".
_REMOTE_QUALIFIER = re.compile(
    rf"{_REMOTE_TOKEN}\s*(?:[\(\[]([^)\]]{{1,40}})[\)\]]|[-–,:]\s*([^|;/\n]{{1,40}}))",
    re.IGNORECASE,
)

# Words that appear in a remote qualifier without naming a place. Anything left over
# after stripping these is treated as a geographic claim. This is what lets the gate
# reject "Remote (South Africa)" without needing an exhaustive list of every country
# on earth, while leaving "Remote-Friendly (Travel-Required)" alone.
_NON_PLACE_TOKENS = frozenset({
    "travel", "required", "require", "friendly", "first", "only", "optional", "flexible",
    "hybrid", "remote", "eligible", "preferred", "based", "work", "from", "home", "full",
    "part", "time", "contract", "temporary", "permanent", "fixed", "term", "days", "day",
    "week", "onsite", "on", "site", "person", "or", "and", "the", "in", "at", "office",
})


def _compile(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE)


def _qualified_remote_patterns(region_pattern: str) -> tuple[re.Pattern[str], re.Pattern[str]]:
    """Build the two shapes a region-qualified remote claim actually takes.

    Forward: "Remote - US", "Remote (United States)", "Remote-Friendly, United States",
    "remote within the United States", "remote anywhere in Europe".
    Backward: "US remote", "United States - Remote", "UK-based remote".

    Both are tight on purpose. A loose window match would fire on "we are a remote-first
    company with offices in San Francisco", which says nothing about eligibility.
    """
    connector = (
        r"[^\w]{0,3}(?:only[^\w]{0,3})?"
        r"(?:(?:in|within|from|across|for|to|based\s+in|anywhere\s+in)[^\w]{0,3})?"
        r"(?:the[^\w]{0,3})?"
    )
    forward = _compile(rf"{_REMOTE_TOKEN}{connector}(?:{region_pattern})")
    backward = _compile(rf"(?:{region_pattern})[^\w]{{0,3}}(?:[- ]?based[^\w]{{0,3}})?{_REMOTE_TOKEN}")
    return forward, backward


# Residency and work-authorisation clauses. {r} is substituted with a region pattern.
_RESIDENCY_TEMPLATES = (
    r"(?:must|required to|need to|expected to|you will|you must|candidates? must)"
    r"[^.;]{0,70}(?:reside|be located|be based|live|relocate)[^.;]{0,70}(?:%R%)",
    r"(?:authoriz|authoris)\w*\s+to\s+work\s+in\s+(?:the\s+)?(?:%R%)",
    r"(?:legally\s+)?(?:eligible|authorised|authorized|permitted|right)\s+to\s+work\s+in\s+(?:the\s+)?(?:%R%)",
    r"(?:%R%)\s+(?:work\s+)?(?:authoriz|authoris)\w*",
    r"(?:open|available|limited|restricted)\s+(?:only\s+)?to\s+(?:candidates?|applicants?|residents?)"
    r"[^.;]{0,40}(?:%R%)",
    r"this (?:role|position|job) is (?:based|located)\s+in\s+(?:the\s+)?(?:%R%)",
    r"relocat\w+\s+to\s+(?:the\s+)?(?:%R%)",
    r"on-?site\s+(?:in|at)\s+(?:the\s+|our\s+)?(?:%R%)",
)

# Onsite / hybrid language. These are caveats unless a named excluded region is in
# the same clause, because "some time in an office" is boilerplate at most companies.
_ONSITE_CAVEAT_PATTERNS = tuple(
    _compile(p) for p in (
        r"\bhybrid\b",
        r"\d+\s*\+?\s*days?\s*(?:per|a|each)\s*week[^.;]{0,30}office",
        r"in\s+(?:one\s+of\s+)?our\s+offices?[^.;]{0,40}\d+\s*%",
        r"\bin[- ]person\b",
        r"\bon-?site\b",
    )
)

# Timezone language. A foreign zone is a caveat; a foreign zone stated as a
# requirement is a block.
_FOREIGN_TZ = _compile(
    r"(?-i:\b(?:PST|PDT|EST|EDT|CST|CDT|MST|MDT|GMT|CET|CEST|BST)\b)|"
    r"(?:pacific|eastern|central|mountain)\s+(?:standard\s+|daylight\s+)?time"
)
_TZ_REQUIREMENT = _compile(
    r"(?:must|required|need)[^.;]{0,60}(?:time ?zone|overlap)|"
    r"(?:overlap|working hours)[^.;]{0,40}(?-i:\b(?:PST|PDT|EST|EDT|CET|GMT)\b)"
)
_LOCAL_TZ = _compile(r"(?-i:\b(?:AEST|AEDT|AET)\b)|australian (?:eastern|time)|sydney time|melbourne time")

# Anything this gate structurally cannot see. Returned on every verdict so the
# limitation travels with the answer instead of living in a docstring nobody reads.
CANNOT_CATCH = (
    "Eligibility rules that exist only in the ATS application form or in structured "
    "fields the public board API does not expose.",
    "Employer-of-record limits phrased without a country list, for example "
    "'we can only hire where we have an entity'.",
    "Unstated recruiter preference: a posting advertised Worldwide where the hiring "
    "manager silently wants US-hours overlap.",
    "Restrictions that live in a linked PDF, a benefits page, or a careers-site "
    "footer rather than in the description text.",
    "Non-English postings, and any paraphrase outside this module's vocabulary.",
    "Anything at all when no description was fetched: a location-only verdict is weak "
    "by construction and is reported at low confidence.",
)


@dataclass
class RemoteVerdict:
    """The outcome of the hard remote gate for one role.

    ``ok`` is what a caller filters on. ``verdict`` says why: PASS means positive
    evidence Alex is eligible, BLOCK means high-confidence evidence he is not, and
    REVIEW means the text is silent, which is not the same as safe.
    """

    ok: bool
    verdict: str                     # PASS | REVIEW | BLOCK
    confidence: str                  # high | medium | low
    reason: str
    evidence: list[str] = field(default_factory=list)
    cannot_catch: tuple[str, ...] = CANNOT_CATCH


def _clean(text: str) -> str:
    """Flatten HTML description bodies to searchable plain text.

    ATS boards return descriptions as HTML. Tags are dropped rather than parsed
    because every rule here is a phrase match, and a real HTML parser would be a
    new dependency for no gain.
    """
    if not text:
        return ""
    out = re.sub(r"<(?:script|style)[^>]*>.*?</(?:script|style)>", " ", text, flags=re.S | re.I)
    out = re.sub(r"<[^>]+>", " ", out)
    out = html.unescape(out)
    return re.sub(r"\s+", " ", out).strip()


def _excerpt(text: str, match: re.Match[str], width: int = 90) -> str:
    """A short, quotable window around a match, for the evidence list."""
    start = max(0, match.start() - width // 2)
    end = min(len(text), match.end() + width // 2)
    return ("..." if start else "") + text[start:end].strip() + ("..." if end < len(text) else "")


def _region_hits(text: str, regions: tuple[tuple[str, str], ...]) -> list[str]:
    return [label for label, pattern in regions if _compile(pattern).search(text)]


def assess_remote(
    role: Role,
    *,
    filters: dict,
    description: str = "",
) -> RemoteVerdict:
    """Decide whether Alex is actually eligible for this role, reading the body text.

    Runs the existing ``_location_ok`` first so the allow and exclude lists stay a
    single source of truth, then applies four text rules that substring matching
    cannot express: region-qualified remote, residency and work-authorisation
    clauses, onsite requirements tied to a named excluded region, and timezone
    requirements.

    Args:
        role: the candidate. ``workplace`` (the board's arrangement field, scored
            ahead of the location string), ``location`` and ``snippet`` are read.
        filters: the ``[filters]`` config block.
        description: the full posting body. Falls back to ``role.description`` when an
            upstream ATS stage attached one, then to the snippet. Without a body the
            verdict is location-only and is reported at low confidence.

    Returns:
        A RemoteVerdict. Callers filter on ``ok``; ``verdict`` distinguishes
        positively-eligible from merely-not-disproven.
    """
    body = _clean(description or getattr(role, "description", "") or "")
    location = (role.location or "").strip()
    snippet = (role.snippet or "").strip()
    # The location field is authoritative and short, so it is searched separately
    # from the body: evidence found there deserves higher confidence.
    haystacks = [("location", location), ("body", " ".join(p for p in (snippet, body) if p))]

    evidence: list[str] = []
    # (priority, text): priority 0 is a direct statement of the rule ("you will be
    # based in Texas"), 3 is the weakest inference. The lowest-priority block leads
    # the reason so Alex reads the strongest evidence first.
    blocks: list[tuple[int, str]] = []
    caveats: list[str] = []

    # Rule 0a: the board's own arrangement field, scored ahead of the location
    # string. This is structured data the company set (workplaceType, Location
    # Type), not prose to be parsed, so it outranks any location: Lyrebird's
    # Forward Deployed Engineer read "Melbourne", an allowed city, while its Lever
    # workplaceType said onsite. Onsite and hybrid both fail the remote-first hard
    # requirement. A self-contradictory label (Ashby's "Hybrid, remote-flagged")
    # is a caveat, not a block, because the board is disagreeing with itself. The
    # location-string rules below remain the fallback for postings whose board set
    # no arrangement field at all.
    workplace = _workplace_label(role)
    if workplace:
        if _WP_ONSITE.search(workplace):
            blocks.append((0, f"the board's own arrangement field says onsite: {workplace!r}"))
        elif _WP_HYBRID.search(workplace) and not _WP_REMOTE.search(workplace):
            blocks.append((0, f"the board's own arrangement field says hybrid: {workplace!r}"))
        elif _WP_HYBRID.search(workplace):
            caveats.append(f"arrangement field disagrees with itself ({workplace!r}): remote-flagged but hybrid")
        elif _WP_REMOTE.search(workplace):
            evidence.append(f"the board's own arrangement field says remote: {workplace!r}")

    # Rule 0: the existing substring filter. Its failures are high-confidence, with one
    # exception worth carving out. A posting listing two cities ("Sydney, Melbourne",
    # a real Cognition Deployed Engineer role) is offering a choice of office, and the
    # exclude list is meant to kill Sydney-mandatory roles, not Sydney-or-Melbourne
    # ones. A country-level allowed token does NOT rescue it, because "Sydney,
    # Australia" is one location, not two: only a named allowed city counts.
    if not _location_ok(location, filters):
        rescuing_city = _ALLOWED_CITY.search(location)
        if rescuing_city:
            caveats.append(
                f"location pairs an excluded site with {rescuing_city.group(0)}, so it reads as a "
                f"choice of office: {location!r}"
            )
        else:
            blocks.append((2, f"location field fails the allow/exclude lists: {location!r}"))

    for label, pattern in _EXCLUDED_REGIONS:
        forward, backward = _qualified_remote_patterns(pattern)
        for where, hay in haystacks:
            if not hay:
                continue
            for rx in (forward, backward):
                m = rx.search(hay)
                if not m:
                    continue
                window = hay[max(0, m.start() - 60): m.end() + 60]
                # An allowed region in the same window means the posting lists
                # several regions, one of which Alex can take. Not a block.
                if _region_hits(window, _ALLOWED_REGIONS):
                    caveats.append(f"remote qualified to {label} but an allowed region sits alongside it")
                    continue
                blocks.append((1, f"remote is qualified to {label} in the {where}: \"{_excerpt(hay, m)}\""))
                break

    # Rule 1b: remote qualified to an unrecognised place. The exclude list can only
    # name regions Alex thought of; "Remote (South Africa)" and "Remote - Brussels"
    # are both real postings that sail through it. Rather than trying to enumerate
    # every country, this treats any place-like qualifier as disqualifying unless the
    # location field also names somewhere Alex can work.
    location_allows = _region_hits(location, _ALLOWED_REGIONS)
    if location and not location_allows:
        for m in _REMOTE_QUALIFIER.finditer(location):
            qualifier = (m.group(1) or m.group(2) or "").strip()
            # Split on hyphens too: "Travel-Required" is two stop words, not a place.
            words = re.findall(r"[A-Za-z]{2,}", qualifier)
            place_words = [wd for wd in words if wd.lower() not in _NON_PLACE_TOKENS]
            if place_words:
                blocks.append((
                    3,
                    f"remote is qualified to an unrecognised place ({qualifier!r}) and the "
                    f"location field names nowhere Alex can work",
                ))

    # Rule 1c: an excluded region named anywhere in the location field, with no
    # allowed region alongside it. The location field is short and authoritative, so
    # proximity to the word "remote" is not required. Skipping this when an allowed
    # region is present is what keeps "Perth, WA, Australia" from tripping the
    # Washington state code.
    if location and not location_allows:
        named = _region_hits(location, _EXCLUDED_REGIONS)
        if named:
            blocks.append((1, f"location field names {', '.join(named)} and nowhere Alex can work: {location!r}"))

    # Rule 2: residency and work-authorisation clauses anywhere in the text.
    for label, pattern in _EXCLUDED_REGIONS:
        for template in _RESIDENCY_TEMPLATES:
            rx = _compile(template.replace("%R%", pattern))
            for where, hay in haystacks:
                m = rx.search(hay) if hay else None
                if m:
                    blocks.append((0, f"{label} residency or work-authorisation requirement: \"{_excerpt(hay, m)}\""))
                    break

    # Rule 3: onsite and hybrid expectations. Only a block when a named excluded
    # region shares the clause, because office-presence boilerplate is near universal.
    for where, hay in haystacks:
        if not hay:
            continue
        for rx in _ONSITE_CAVEAT_PATTERNS:
            m = rx.search(hay)
            if not m:
                continue
            clause = hay[max(0, m.start() - 120): m.end() + 120]
            near = _region_hits(clause, _EXCLUDED_REGIONS)
            if near and not _region_hits(clause, _ALLOWED_REGIONS):
                blocks.append((0, f"onsite requirement tied to {', '.join(near)}: \"{_excerpt(hay, m)}\""))
            else:
                caveats.append(f"onsite or hybrid expectation in the {where}: \"{_excerpt(hay, m)}\"")
            break

    # Rule 3b: travel load stated as a share of working time. Living in the body
    # text past the snippet cut is exactly how Talkdesk's "travel approximately
    # 35-70% of the time" slipped through, which is why the gate reads the full
    # description. At or above _TRAVEL_BLOCK_PCT the role is a field role, not a
    # remote one; below that it is a caveat worth reading.
    for where, hay in haystacks:
        if not hay:
            continue
        for m in _TRAVEL_NEAR.finditer(hay):
            pct = max(int(g) for g in m.groups() if g)
            if pct >= _TRAVEL_BLOCK_PCT:
                blocks.append((0, f"travel load of {pct}% stated in the {where}: \"{_excerpt(hay, m)}\""))
            else:
                caveats.append(f"travel load of {pct}% stated in the {where}: \"{_excerpt(hay, m)}\"")
            break

    # Rule 4: timezone. A foreign zone alone is a caveat; a foreign zone stated as a
    # requirement is a de facto exclusion for someone in AEST.
    for where, hay in haystacks:
        if not hay:
            continue
        if _LOCAL_TZ.search(hay):
            evidence.append(f"Australian timezone named in the {where}")
            continue
        m = _TZ_REQUIREMENT.search(hay)
        if m and _FOREIGN_TZ.search(hay):
            blocks.append((0, f"foreign timezone stated as a requirement: \"{_excerpt(hay, m)}\""))
        elif _FOREIGN_TZ.search(hay):
            caveats.append(f"foreign timezone mentioned in the {where}")

    allowed_in_location = _region_hits(location, _ALLOWED_REGIONS)
    allowed_in_body = _region_hits(haystacks[1][1], _ALLOWED_REGIONS)
    if allowed_in_location:
        evidence.append(f"allowed region in the location field: {', '.join(allowed_in_location)}")
    elif allowed_in_body:
        evidence.append(f"allowed region in the body only: {', '.join(allowed_in_body)}")

    # De-duplicate while preserving order: several rules often quote the same clause.
    blocks = sorted(dict.fromkeys(blocks), key=lambda b: b[0])
    block_texts = [text for _, text in blocks]
    caveats = list(dict.fromkeys(caveats))

    if blocks:
        return RemoteVerdict(
            ok=False,
            verdict="BLOCK",
            confidence="high",
            reason="not remote-eligible for Alex: " + block_texts[0],
            evidence=block_texts + caveats,
        )

    if not body:
        # Without a body the gate cannot see hidden US-only or onsite rules. An
        # explicit allowed region in the location field is still real evidence, so
        # that case is a medium-confidence PASS rather than a shrug.
        return RemoteVerdict(
            ok=_location_ok(location, filters),
            verdict="PASS" if allowed_in_location else "REVIEW",
            confidence="medium" if allowed_in_location else "low",
            reason=(
                f"allowed region in the location field ({', '.join(allowed_in_location)}), but no "
                f"description was fetched, so hidden US-only or onsite rules are unchecked"
                if allowed_in_location else
                "no description fetched, so this is a location-only verdict and proves nothing "
                "about hidden US-only or onsite rules"
            ),
            evidence=evidence + caveats,
        )

    if allowed_in_location:
        return RemoteVerdict(
            ok=True,
            verdict="PASS",
            confidence="medium" if caveats else "high",
            reason="allowed region stated in the location field and no exclusion found in the body",
            evidence=evidence + caveats,
        )

    if allowed_in_body:
        return RemoteVerdict(
            ok=True,
            verdict="PASS",
            confidence="medium",
            reason="allowed region appears in the body but not the location field, so confirm before applying",
            evidence=evidence + caveats,
        )

    return RemoteVerdict(
        ok=True,
        verdict="REVIEW",
        confidence="low",
        reason="nothing in the text excludes Alex, but nothing confirms AU eligibility either",
        evidence=evidence + caveats,
    )


def remote_gate(
    roles: list[Role],
    *,
    filters: dict,
    descriptions: dict[str, str] | None = None,
    allow_review: bool = True,
) -> tuple[list[Role], list[tuple[Role, RemoteVerdict]]]:
    """Split roles into kept and rejected using ``assess_remote``.

    ``allow_review=True`` keeps REVIEW roles, which is the right default: a silent
    posting is usually just a terse posting, and dropping it deletes the role before
    Alex ever sees it. Set it False for a strict pass that only keeps positively
    confirmed AU-eligible roles.

    Returns ``(kept, rejected)`` where each rejected entry carries its verdict, so a
    digest can show what was dropped and why.
    """
    descriptions = descriptions or {}
    kept: list[Role] = []
    rejected: list[tuple[Role, RemoteVerdict]] = []
    for role in roles:
        verdict = assess_remote(role, filters=filters, description=descriptions.get(role.id, ""))
        role.remote_verdict = verdict  # type: ignore[attr-defined]
        if verdict.ok and (allow_review or verdict.verdict == "PASS"):
            kept.append(role)
        else:
            rejected.append((role, verdict))
    return kept, rejected


# --------------------------------------------------------------------------
# Low-contest proxy
# --------------------------------------------------------------------------

# Sites that redistribute postings. A hit here means the posting has escaped the
# company's own board and is being seen by an aggregator-sized audience.
AGGREGATOR_DOMAINS = (
    "linkedin.com", "seek.com.au", "indeed.com", "glassdoor.com", "ziprecruiter.com",
    "simplyhired.com", "adzuna.com", "jora.com", "jobgether.com", "builtin.com",
    "wellfound.com", "angel.co", "otta.com", "welcometothejungle.com", "dice.com",
    "monster.com", "careerjet.com", "talent.com", "remoteok.com", "weworkremotely.com",
    "himalayas.app", "jobicy.com", "arc.dev", "levels.fyi", "trueup.io", "employbl.com",
    "hiring.cafe", "jobright.ai", "lensa.com", "whatjobs.com", "ycombinator.com",
)

# Companies whose name alone pulls a flood of applications. Alex still wants several
# of these, which is exactly why this signal measures contest and never desirability.
_HOUSEHOLD_AI_NAMES = (
    "anthropic", "openai", "google", "google deepmind", "deepmind", "meta", "microsoft",
    "apple", "amazon", "nvidia", "canva", "atlassian", "stripe", "figma", "netflix",
    "spotify", "airbnb", "uber", "tesla", "xai", "mistral", "cohere", "hugging face",
    "databricks", "snowflake", "palantir", "scale ai", "perplexity", "cursor", "anysphere",
)

_SPECIFIC_TITLE_PHRASES = (
    "forward deployed", "forward-deployed", "applied ai", "applied ml", "ai solutions",
    "solutions architect", "deployed engineer", "agent engineer", "llm engineer",
    "customer engineer", "field engineer", "ai-native", "ai native", "solutions engineer",
    "sales engineer", "developer advocate", "delivery engineer", "implementation engineer",
)
_GENERIC_TITLE_PHRASES = (
    "software engineer", "software developer", "full stack", "fullstack", "backend engineer",
    "frontend engineer", "front-end engineer", "back-end engineer", "web developer",
    "data scientist", "data analyst", "product manager", "program manager",
)


@dataclass
class SignalComponent:
    """One input to the proxy, kept separable so Alex can argue with it individually.

    ``value`` is None when the component could not be measured, which removes both
    its points and its weight from the score rather than silently scoring it zero.
    """

    name: str
    weight: float
    value: float | None
    detail: str

    @property
    def points(self) -> float:
        return 0.0 if self.value is None else self.value * self.weight

    @property
    def measured(self) -> bool:
        return self.value is not None


@dataclass
class ContestSignal:
    """A proxy for how under-contested a role is likely to be. Not an applicant count.

    ``proxy_score`` is 0-100 where higher means *probably fewer applicants*. It is
    renormalised over the weight that was actually measurable, so a run without the
    aggregator probe still produces a comparable number, flagged by
    ``measured_weight``.
    """

    proxy_score: int | None
    band: str
    components: list[SignalComponent]
    reason: str
    caveats: list[str] = field(default_factory=list)
    measured_weight: float = 0.0

    @property
    def is_proxy(self) -> bool:
        """Always True. Exists so no caller can render this without saying so."""
        return True

    def explain(self) -> str:
        """Multi-line breakdown for a digest or a CLI dump."""
        head = f"{self.band} | low-contest proxy {self.proxy_score}/100 (proxy, not an applicant count)"
        lines = [head, f"  why: {self.reason}"]
        for c in self.components:
            val = "unmeasured" if c.value is None else f"{c.value:.2f}"
            lines.append(f"  - {c.name} (weight {c.weight:g}): {val}  {c.detail}")
        for note in self.caveats:
            lines.append(f"  ! {note}")
        return "\n".join(lines)


@dataclass
class AggregatorProbe:
    """Result of asking a web search whether a posting has escaped the company board."""

    hits: int
    domains: list[str]
    query: str


# Markers of a throttle or captcha page rather than real results. Treating one of
# these as "no aggregators found" would be the single most dangerous bug in this
# module, because it fabricates the strongest low-contest signal out of a failure.
_SEARCH_BLOCKED = re.compile(r"anomaly|unfortunately, bots|captcha|are you a robot", re.IGNORECASE)


def _throttle() -> None:
    """Space out network calls so a batch run never hammers a search endpoint."""
    global _last_network_call
    wait = POLITE_DELAY_SECONDS - (time.monotonic() - _last_network_call)
    if wait > 0:
        time.sleep(wait)
    _last_network_call = time.monotonic()


def duckduckgo_aggregator_probe(role: Role) -> AggregatorProbe | None:
    """Best-effort check of whether this exact posting is visible on aggregators.

    Searches the quoted title plus the company and counts result domains that are
    job aggregators. Zero aggregator hits is the strongest low-contest evidence
    available without an applicant count: the posting exists on the company board
    and nowhere a job seeker browses.

    Returns None on any failure, including a blocked or rate-limited endpoint, so a
    caller treats the component as unmeasured rather than as a zero. Never raises.
    """
    try:
        import httpx

        _throttle()
        query = f'"{role.title}" {role.company}'
        response = httpx.post(
            "https://html.duckduckgo.com/html/",
            data={"q": query},
            headers={"User-Agent": USER_AGENT},
            timeout=TIMEOUT_SECONDS,
            follow_redirects=True,
        )
        # DuckDuckGo answers scripted traffic with a 202 anomaly page, and sometimes a
        # 200 one. Both must read as "unavailable", never as "zero aggregators", or the
        # proxy would award a perfect score to every role the moment it got throttled.
        if response.status_code != 200 or _SEARCH_BLOCKED.search(response.text):
            return None
        raw_links = re.findall(r'result__a"\s+href="([^"]+)"', response.text)
        domains: list[str] = []
        for link in raw_links:
            url = html.unescape(link)
            if url.startswith("//duckduckgo.com/l/"):
                params = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
                url = params.get("uddg", [url])[0]
            host = urllib.parse.urlsplit(url).netloc.lower()
            for agg in AGGREGATOR_DOMAINS:
                if agg in host and agg not in domains:
                    domains.append(agg)
        return AggregatorProbe(hits=len(domains), domains=domains, query=query)
    except Exception:  # noqa: BLE001 - a dead search endpoint must never abort a run
        return None


def make_llm_aggregator_probe(client, *, timeout: int = 90):
    """Build an aggregator probe backed by Scout's existing web-capable LLM client.

    The DuckDuckGo probe is free but throttles hard: it answers scripted traffic with
    a 202 anomaly page, which this module correctly reads as "unmeasured" rather than
    "zero hits", but that means the strongest component drops out of most batch runs.
    Scout already owns a reliable web path in ``ClaudeCliClient``, so this reuses it.

    The prompt asks for a JSON array of aggregator domains and nothing else. Anything
    unparseable returns None, so the component stays honestly unmeasured rather than
    guessing. Never raises.
    """

    def probe(role: Role) -> AggregatorProbe | None:
        query = f'"{role.title}" {role.company}'
        prompt = (
            "Search the web for this exact job posting and report only where it is "
            "re-listed.\n\n"
            f"Job title: {role.title}\nCompany: {role.company}\nPosting URL: {role.url}\n\n"
            "Return ONLY a strict JSON array of the job-aggregator domains that carry "
            "this specific posting, for example [\"linkedin.com\", \"seek.com.au\"]. "
            "Return [] if the posting appears only on the company's own careers site or "
            "applicant tracking system. Do not count the company's own domain, and do "
            "not include any prose."
        )
        try:
            raw = client.complete(prompt, web=True, timeout=timeout)
            import json

            match = re.search(r"\[.*\]", raw, re.S)
            if not match:
                return None
            found = json.loads(match.group(0))
            if not isinstance(found, list):
                return None
            domains = [
                agg for agg in AGGREGATOR_DOMAINS
                if any(agg in str(item).lower() for item in found)
            ]
            return AggregatorProbe(hits=len(domains), domains=domains, query=query)
        except Exception:  # noqa: BLE001 - an unmeasured component beats a wrong one
            return None

    return probe


def _freshness_value(age_days: float | None) -> tuple[float | None, str]:
    """Score freshness on a U-shape, not a straight line.

    Applications arrive front-loaded: the bulk land in the first week, so cumulative
    contest is concave and the low-contest value has to fall fastest early, not
    evenly. Freshness also correlates with visibility, which is the counter-pressure:
    a brand-new posting is the one currently sitting at the top of every feed. That
    is why day 0 scores high but not perfectly.

    Past roughly forty-five days the curve turns back up, because a req still open
    that long usually means the funnel is not converting, which is genuinely less
    effective contest for a strong candidate. The recovery is capped well below the
    fresh peak because the same signal is also the signature of a ghost job.
    """
    if age_days is None:
        return None, "posting date unknown"
    d = max(0.0, age_days)
    if d <= 2:
        return 0.95, f"{d:.0f} days old, almost nobody has seen it yet"
    if d < 7:
        return 0.95 - (d - 2) * (0.20 / 5), f"{d:.0f} days old, inside the front-loaded application window"
    if d < 14:
        return 0.75 - (d - 7) * (0.30 / 7), f"{d:.0f} days old, the first application wave has landed"
    if d < 30:
        return 0.45 - (d - 14) * (0.25 / 16), f"{d:.0f} days old, well past the peak application window"
    if d < 45:
        return 0.20, f"{d:.0f} days old, saturated"
    return min(0.35, 0.20 + (d - 45) * 0.003), (
        f"{d:.0f} days old and still open, which often means the funnel is not converting "
        f"(capped: this is also what a ghost job looks like)"
    )


def _aggregator_value(probe: AggregatorProbe | None) -> tuple[float | None, str]:
    """Convert aggregator visibility into a low-contest value.

    Absence is the point. Each additional aggregator carrying the posting multiplies
    the audience, so the value drops steeply from zero hits rather than linearly.
    """
    if probe is None:
        return None, "aggregator probe unavailable, component excluded from the score"
    if probe.hits == 0:
        return 1.0, "no aggregator carries this posting in web search results"
    ladder = {1: 0.55, 2: 0.30}
    value = ladder.get(probe.hits, 0.10)
    return value, f"carried by {probe.hits} aggregator(s): {', '.join(probe.domains)}"


def _obscurity_value(
    company: str,
    board_size: int | None,
    famous: tuple[str, ...],
) -> tuple[float | None, str]:
    """Company obscurity as a contest signal, deliberately blind to desirability.

    A household name pulls thousands of applications for the same job an unknown
    company struggles to fill. Board size is the secondary proxy: a nine-posting
    board is a small company, and small companies get less inbound. Alex wants
    several of the household names anyway, which is why this score never touches
    whether he should apply, only how crowded it will be.
    """
    name = (company or "").strip().lower()
    if not name:
        return None, "company name missing, component excluded from the score"
    if any(f in name or name in f for f in famous if f):
        return 0.15, f"{company} is a household name, expect very heavy inbound"
    if board_size is None:
        return 0.70, f"{company} is not a household name and board size is unknown"
    if board_size < 25:
        return 0.90, f"{company} runs a {board_size}-posting board, small and low-profile"
    if board_size < 100:
        return 0.75, f"{company} runs a {board_size}-posting board, mid-size"
    if board_size < 400:
        return 0.55, f"{company} runs a {board_size}-posting board, large enough to attract inbound"
    return 0.40, f"{company} runs a {board_size}-posting board, a large and visible employer"


def _title_specificity_value(title: str) -> tuple[float | None, str]:
    """Specific titles self-select a smaller applicant pool.

    "Forward Deployed Engineer" is a term most engineers do not search for.
    "Software Engineer" is the single most applied-to title in the market. A title
    carrying both is treated as diluted, because the generic half is what pulls
    the crowd.
    """
    hay = (title or "").lower().strip()
    if not hay:
        return None, "title missing, component excluded from the score"
    specific = [p for p in _SPECIFIC_TITLE_PHRASES if p in hay]
    generic = [p for p in _GENERIC_TITLE_PHRASES if p in hay]
    if specific and not generic:
        return 0.95, f"niche title: {', '.join(specific)}"
    if specific and generic:
        return 0.60, f"mixed title: niche ({', '.join(specific)}) plus generic ({', '.join(generic)})"
    if generic:
        return 0.20, f"high-volume generic title: {', '.join(generic)}"
    return 0.50, "title matches neither the niche nor the high-volume vocabulary"


def _location_narrowness_value(location: str) -> tuple[float | None, str]:
    """A narrower advertised location means a smaller pool competing for the seat.

    This is the component most in tension with Alex's own interest: worldwide-remote
    roles are the ones he is most often eligible for, and they are also the ones the
    entire planet applies to. The tension is real and is left visible rather than
    smoothed away.
    """
    hay = (location or "").lower().strip()
    if not hay:
        return None, "location missing, component excluded from the score"
    if re.search(r"\baustralia|\bmelbourne|\bvictoria|\bbrisbane|\bperth|\badelaide|\banz\b|\bau\b", hay):
        return 0.95, f"Australia-scoped location ({location}), the smallest applicant pool available"
    if re.search(r"\bapac\b|asia[- ]pacific", hay):
        return 0.70, f"APAC-scoped location ({location})"
    if re.search(r"\bworldwide|\bglobal|\banywhere", hay):
        return 0.25, f"worldwide-scoped location ({location}), contested by the entire planet"
    if hay.count(",") >= 2 or "|" in hay or ";" in hay:
        return 0.50, f"multi-site location ({location})"
    return 0.45, f"single named location with no regional scope ({location})"


# Weights sum to 100. Aggregator absence carries the most because it is the only
# component that measures observed visibility rather than inferring it.
DEFAULT_WEIGHTS = {
    "aggregator_absence": 30.0,
    "freshness": 20.0,
    "company_obscurity": 20.0,
    "title_specificity": 15.0,
    "location_narrowness": 15.0,
}

BAND_QUIET = "LIKELY QUIET (proxy)"
BAND_MODERATE = "MODERATE (proxy)"
BAND_CROWDED = "LIKELY CROWDED (proxy)"
BAND_UNMEASURED = "UNMEASURED"


def contest_signal(
    role: Role,
    *,
    age_days: float | None = None,
    board_size: int | None = None,
    aggregator: AggregatorProbe | None = None,
    famous_companies: tuple[str, ...] = _HOUSEHOLD_AI_NAMES,
    weights: dict[str, float] | None = None,
) -> ContestSignal:
    """Compute the transparent low-contest proxy for one role.

    This never estimates an applicant count and never will. It combines five
    observable properties into a 0-100 number whose only claim is "probably fewer
    people are competing for this than for a role scoring lower". Every component is
    returned with its raw value and its reasoning so the number can be argued with.

    Args:
        role: the candidate. ``title``, ``company`` and ``location`` are read.
        age_days: days since the posting was first published. Falls back to
            ``role.age_days`` when an ATS stage attached one. None leaves freshness
            unmeasured rather than guessed.
        board_size: number of live postings on the company's board, a cheap proxy for
            company size that the ATS fetch already has in hand.
        aggregator: result of an aggregator probe. Pass None to run without network.
        famous_companies: names treated as household. Override to include Alex's own
            target list so it stays honest as the market moves.
        weights: override DEFAULT_WEIGHTS to re-weight the proxy.

    Returns:
        A ContestSignal. ``proxy_score`` is None only when nothing was measurable.
    """
    w = {**DEFAULT_WEIGHTS, **(weights or {})}
    if age_days is None:
        age_days = getattr(role, "age_days", None)
    if board_size is None:
        board_size = getattr(role, "board_size", None)

    caveats: list[str] = []

    fresh_v, fresh_d = _freshness_value(age_days)
    agg_v, agg_d = _aggregator_value(aggregator)
    obsc_v, obsc_d = _obscurity_value(role.company, board_size, famous_companies)
    title_v, title_d = _title_specificity_value(role.title)
    loc_v, loc_d = _location_narrowness_value(role.location)

    components = [
        SignalComponent("aggregator_absence", w["aggregator_absence"], agg_v, agg_d),
        SignalComponent("freshness", w["freshness"], fresh_v, fresh_d),
        SignalComponent("company_obscurity", w["company_obscurity"], obsc_v, obsc_d),
        SignalComponent("title_specificity", w["title_specificity"], title_v, title_d),
        SignalComponent("location_narrowness", w["location_narrowness"], loc_v, loc_d),
    ]

    measured_weight = sum(c.weight for c in components if c.measured)
    if measured_weight <= 0:
        return ContestSignal(
            proxy_score=None,
            band=BAND_UNMEASURED,
            components=components,
            reason="no component could be measured for this role",
            caveats=["nothing was measurable, so no proxy was produced"],
            measured_weight=0.0,
        )

    score = round(100 * sum(c.points for c in components) / measured_weight)

    if measured_weight < 60:
        caveats.append(
            f"only {measured_weight:g} of {sum(w.values()):g} weight was measurable, "
            f"so this score is thinner than it looks"
        )
    if agg_v is None:
        caveats.append("the strongest signal (aggregator absence) was not measured")
    if age_days is not None and age_days >= 45:
        caveats.append("a long-open posting can mean a stalled funnel or a ghost job, and this cannot tell them apart")
    if loc_v is not None and loc_v <= 0.30:
        caveats.append("worldwide-remote scores low on contest but is often where Alex is most eligible, so read this one against the fit score")
    caveats.append("this is a proxy for contest, not a measured applicant count: no board publishes one")

    band = BAND_QUIET if score >= 70 else BAND_MODERATE if score >= 45 else BAND_CROWDED

    top = sorted((c for c in components if c.measured), key=lambda c: c.points, reverse=True)[:3]
    reason = "; ".join(c.detail for c in top)

    return ContestSignal(
        proxy_score=score,
        band=band,
        components=components,
        reason=reason,
        caveats=caveats,
        measured_weight=measured_weight,
    )


def rank_by_contest(
    roles: list[Role],
    *,
    probe=None,
    board_sizes: dict[str, int] | None = None,
    ages: dict[str, float] | None = None,
    famous_companies: tuple[str, ...] = _HOUSEHOLD_AI_NAMES,
) -> list[tuple[Role, ContestSignal]]:
    """Attach a contest signal to every role and return them most-quiet first.

    ``probe`` is injected (pass ``duckduckgo_aggregator_probe`` to go live, omit it to
    stay offline) so this whole path is testable without network, matching how every
    other Scout stage handles its outside world. A probe that raises is caught here
    too: one dead lookup must not lose the rest of the batch.

    The signal is attached to each role as ``role.contest`` so downstream filter,
    score, draft and track stages keep working on unmodified Role objects.
    """
    board_sizes = board_sizes or {}
    ages = ages or {}
    out: list[tuple[Role, ContestSignal]] = []
    for role in roles:
        result = None
        if probe is not None:
            try:
                result = probe(role)
            except Exception:  # noqa: BLE001 - one dead lookup must not lose the batch
                result = None
        signal = contest_signal(
            role,
            age_days=ages.get(role.id),
            board_size=board_sizes.get(role.company.strip().lower()),
            aggregator=result,
            famous_companies=famous_companies,
        )
        role.contest = signal  # type: ignore[attr-defined]
        out.append((role, signal))
    return sorted(out, key=lambda pair: (pair[1].proxy_score is None, -(pair[1].proxy_score or 0)))
