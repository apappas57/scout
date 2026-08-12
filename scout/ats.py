"""ATS stage: enumerate company-owned applicant tracking boards directly.

WHY this exists alongside discover.py. The discover stage asks an LLM with web
access to find roles, so it can only surface postings that a search engine has
already indexed, which is the same pool that Seek and LinkedIn aggregate. By the
time a role is indexed it is high-volume. A company's own ATS board is where a
posting appears first and where it is cheapest to be early. This module talks to
those boards directly over their public JSON endpoints. No LLM is involved
anywhere, so the result is deterministic, free, and repeatable.

It is a counterpart to discover.py, not a replacement. The runner uses both and
merges the results before the verify stage.

Supported boards, all verified live against real companies with no API key:

    greenhouse       https://boards-api.greenhouse.io/v1/boards/{slug}/jobs
    lever            https://api.lever.co/v0/postings/{slug}?mode=json
    ashby            https://api.ashbyhq.com/posting-api/job-board/{slug}
    smartrecruiters  https://api.smartrecruiters.com/v1/companies/{slug}/postings

Workable is deliberately absent. Its public widget endpoint
(apply.workable.com/api/v1/widget/accounts/{slug}) was probed against seven real
slugs: it either 404s or returns 200 with an empty jobs array, and the v3
endpoint answers with a bot challenge. It could not be verified to return live
postings, so it is not implemented. A board that silently returns nothing is
worse than no board at all, because it reads as "this company is not hiring".

Design rules this module holds to:

- Every network call is non-fatal. One dead board, one bad slug, or one company
  that renamed its board must never abort a run. Failures are logged and skipped.
- Locations are extracted faithfully and never invented. See UNKNOWN_LOCATION.
- Title matching is configurable, not hardcoded, and deliberately a little
  generous, because the downstream filter and score stages tighten it further.
"""
from __future__ import annotations

import html
import json
import logging
import re
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from collections.abc import Callable, Iterable, Mapping, Sequence

from scout.models import Role

log = logging.getLogger("scout.ats")


# A real, honest User-Agent. These are public endpoints being used as intended,
# so identify the client rather than impersonating a browser.
USER_AGENT = "scout/0.1 (personal job-search agent; contact via job application)"

DEFAULT_TIMEOUT = 20.0

# Seconds between calls. Small, but enough that a 30-company registry does not
# look like a scrape to any one board.
DEFAULT_DELAY = 0.4

# Cap on detail fetches per company. Descriptions cost one extra request each,
# so this bounds the worst case when a company posts many matching roles.
DEFAULT_MAX_DETAIL = 25

# SmartRecruiters is the only board here that pages. 100 is its maximum page size.
_SR_PAGE_SIZE = 100
_SR_MAX_PAGES = 10

# WHY a sentinel rather than an empty string or a guess: the remote gate in
# filter_dedupe is the single thing standing between Alex and a pile of US-only
# roles. A posting whose board exposes no usable location must stay visibly
# unknown. This string contains no allowed substring (so it never silently
# passes the remote_only gate) and no excluded substring (so the reason it was
# dropped is "unconfirmed", not "wrong country"). Never substitute a guess,
# and in particular never write "Remote" here just because a board set an
# isRemote flag: on Ashby, isRemote is true for postings located in London.
UNKNOWN_LOCATION = "Unknown"

# Default title keywords. Deliberately generous: the filter and score stages
# downstream tighten this, and a missed posting is unrecoverable while an extra
# one costs a line in the digest. Matching is separator-insensitive, so
# "forward deployed" also catches "Forward-Deployed" and "forward/deployed".
DEFAULT_TITLE_KEYWORDS: tuple[str, ...] = (
    "forward deployed",
    "deployed engineer",
    "applied ai",
    "ai engineer",
    "ai solutions",
    "solutions architect",
    "solutions engineer",
    "agent engineer",
    "agentic engineer",
    "llm engineer",
    "genai engineer",
    "generative ai engineer",
    "ai native",
    "customer engineer",
    "implementation engineer",
)

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")

# Snippet length. Long enough for the score stage to find profile keywords and
# for a human to judge the role at a glance, short enough to keep the SQLite
# rows and the digest readable.
_SNIPPET_CHARS = 600


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def httpx_get_json(url: str, *, timeout: float = DEFAULT_TIMEOUT) -> tuple[int, object]:
    """Default fetcher. Returns ``(status_code, parsed_json)``.

    Mirrors verify.httpx_get: any network or parse failure is swallowed into
    ``(0, None)`` so a dead board degrades to "no roles from this company"
    instead of raising into the runner. Injected everywhere below so tests run
    fully offline.
    """
    try:
        import httpx

        response = httpx.get(
            url,
            follow_redirects=True,
            timeout=timeout,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )
    except Exception as exc:  # noqa: BLE001 - non-fatal by design
        log.warning("ats: request failed for %s: %s", url, exc)
        return 0, None
    if response.status_code != 200:
        return response.status_code, None
    try:
        return 200, response.json()
    except (json.JSONDecodeError, ValueError) as exc:
        log.warning("ats: non-JSON body from %s: %s", url, exc)
        return 200, None


# ---------------------------------------------------------------------------
# Text and date helpers
# ---------------------------------------------------------------------------


def _strip_html(raw: str) -> str:
    """Turn a board's HTML description into a plain-text snippet.

    Two unescape passes on purpose. Greenhouse double-encodes its ``content``
    field (``&lt;p&gt;``), so the first pass yields real HTML and the second
    resolves entities left inside the text. Ashby and Lever hand back real HTML,
    where the first pass is a no-op and the second still cleans up ``&amp;`` and
    ``&nbsp;``. Either way the reader never sees a raw entity.
    """
    if not raw:
        return ""
    text = _TAG_RE.sub(" ", html.unescape(raw))
    return _WS_RE.sub(" ", html.unescape(text)).strip()


def _normalise(text: str) -> str:
    """Lowercase and collapse every non-alphanumeric run to one space.

    Lets a single keyword ("forward deployed") match every separator a company
    might use, so the keyword list stays short and readable.
    """
    return _NON_ALNUM_RE.sub(" ", (text or "").lower()).strip()


def title_matches(title: str, keywords: Sequence[str]) -> bool:
    """True when the title plausibly matches one of the target role keywords."""
    hay = _normalise(title)
    if not hay:
        return False
    return any(_normalise(kw) in hay for kw in keywords if kw)


def _join_locations(parts: Iterable[str | None]) -> str:
    """Join every location string a posting lists, in order, without duplicates.

    Semicolon-separated, which is what Greenhouse already uses natively for
    multi-location postings, so the downstream substring filter sees one
    consistent shape. Returns UNKNOWN_LOCATION when the board gave us nothing:
    an absent location is reported as absent, never inferred from a remote flag,
    a department name, or the company's headquarters.
    """
    seen: list[str] = []
    for part in parts:
        cleaned = _clean_location(part)
        if cleaned and cleaned.lower() not in {s.lower() for s in seen}:
            seen.append(cleaned)
    return "; ".join(seen) if seen else UNKNOWN_LOCATION


def _clean_location(part: str | None) -> str:
    """Tidy a single location string without changing what it says.

    SmartRecruiters emits ``"Beijing, , China"`` when a posting has no region,
    so empty comma segments are dropped. Nothing else is rewritten.
    """
    if not part:
        return ""
    segments = [s.strip() for s in str(part).split(",")]
    return ", ".join(s for s in segments if s)


def _iso_date(value) -> str:
    """Normalise a board's published/updated field to ``YYYY-MM-DD``.

    Handles ISO strings (Greenhouse, Ashby, SmartRecruiters) and epoch
    milliseconds (Lever). Returns "" rather than guessing, because the volume
    signal downstream would rather see a missing date than a wrong one.
    """
    if value in (None, ""):
        return ""
    if isinstance(value, (int, float)):
        try:
            # Lever stamps createdAt in epoch milliseconds.
            return datetime.fromtimestamp(value / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        except (OverflowError, OSError, ValueError):
            return ""
    text = str(value).strip()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).strftime("%Y-%m-%d")
    except ValueError:
        # Some boards send a bare date already. Accept it, reject anything else.
        return text[:10] if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text[:10] or "") else ""


def _compose_snippet(*, posted: str, workplace: str, body: str) -> str:
    """Build the snippet, front-loading the facts the digest and score need.

    The posted date and the board's own workplace label lead, because Role has
    no field for either and the snippet is the only part of a Role that survives
    into SQLite. The workplace label is kept out of Role.location on purpose: it
    is the board's claim about working style, not a place, and folding it into
    the location would let "Remote" smuggle a London role past the remote gate.
    """
    lead: list[str] = []
    if posted:
        lead.append(f"Posted {posted}.")
    if workplace:
        lead.append(f"Workplace: {workplace}.")
    text = " ".join(lead + ([body] if body else []))
    return text[:_SNIPPET_CHARS].strip()


def _make_role(
    *,
    company: str,
    title: str,
    url: str,
    location: str,
    board: str,
    posted: str,
    workplace: str,
    body: str,
) -> Role:
    """Build a Role and attach the posting date as a runtime attribute.

    ``posted_at`` follows the precedent set by the runner attaching
    ``result.digest`` to RunResult: Role is a plain dataclass with no __slots__,
    so a caller that wants the raw date can read ``role.posted_at`` without the
    Role contract changing and without any downstream stage being touched. The
    date is also written into the snippet so it survives the trip through
    SQLite, which only persists the declared fields.

    ``workplace`` and ``description`` are declared Role fields and are persisted
    whole. The remote gate scores the board's own arrangement field ahead of the
    location string (Lyrebird's FDE read "Melbourne" while workplaceType said
    onsite), and it reads the full body for clauses like "35-70% travel" that a
    truncated snippet cuts off (Talkdesk's APAC SE, cut at 600 chars).
    """
    role = Role(
        company=company,
        title=title,
        url=url,
        location=location,
        source=f"ats:{board}",
        snippet=_compose_snippet(posted=posted, workplace=workplace, body=body),
        workplace=workplace,
        description=body,
    )
    role.posted_at = posted  # type: ignore[attr-defined]
    return role


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Board:
    """One company's board on one ATS.

    ``company`` is the display name that lands in Role.company, so it should
    read the way Alex would write it. ``slug`` is that company's identifier on
    that specific ATS, which is frequently not the company name lowercased, and
    which must be verified to return 200 before it goes in a registry.
    """

    company: str
    board: str
    slug: str

    @classmethod
    def from_mapping(cls, item: Mapping) -> "Board":
        """Build a Board from a TOML/JSON registry row.

        Raises ValueError on a row that is missing a field or names a board this
        module cannot fetch, so a typo in the registry surfaces as one skipped
        company rather than a silently empty result.
        """
        # Accept both key names. config/ats_companies.toml writes `name`, while
        # the inline `[ats] companies` rows in the private config write `company`.
        # Reading both means neither file has to be rewritten, and a future
        # registry cannot silently produce zero roles because of a key rename:
        # that exact mismatch skipped all 146 entries on the first live run.
        company = str(item.get("company") or item.get("name") or "").strip()
        board = str(item.get("board", "")).strip().lower()
        slug = str(item.get("slug", "")).strip()
        if not company or not board or not slug:
            raise ValueError(f"ats: incomplete registry entry: {dict(item)!r}")
        if board not in FETCHERS:
            raise ValueError(f"ats: unsupported board {board!r} for {company}")
        return cls(company=company, board=board, slug=slug)


def _coerce(entry) -> Board:
    return entry if isinstance(entry, Board) else Board.from_mapping(entry)


# ---------------------------------------------------------------------------
# Per-board fetchers
# ---------------------------------------------------------------------------


def fetch_greenhouse(
    company: str,
    slug: str,
    *,
    keywords: Sequence[str] = DEFAULT_TITLE_KEYWORDS,
    get_json: Callable[..., tuple[int, object]] = httpx_get_json,
    sleep: Callable[[float], None] = time.sleep,
    delay: float = DEFAULT_DELAY,
    timeout: float = DEFAULT_TIMEOUT,
    with_content: bool = True,
    max_detail: int = DEFAULT_MAX_DETAIL,
) -> list[Role]:
    """Fetch and normalise a Greenhouse board.

    The list endpoint is fetched without ``content=true`` on purpose: Anthropic's
    board is 5.5 MB with descriptions and 0.2 MB without, and only a handful of
    the 400 postings will ever match. Descriptions are pulled per matched
    posting instead, which is a smaller total transfer and far fewer wasted bytes.

    Location: ``location.name`` is the authoritative field and already carries
    every location a multi-site posting lists, pipe- or semicolon-separated
    (for example "London, UK; Ontario, CAN; Remote-Friendly, United States").
    It is passed through verbatim. ``offices`` is used only as a fallback when
    ``location.name`` is missing.
    """
    base = f"https://boards-api.greenhouse.io/v1/boards/{urllib.parse.quote(slug)}/jobs"
    status, payload = get_json(base, timeout=timeout)
    if status != 200 or not isinstance(payload, dict):
        log.warning("ats: greenhouse/%s returned %s", slug, status)
        return []

    jobs = payload.get("jobs") or []
    matched = [j for j in jobs if isinstance(j, dict) and title_matches(j.get("title", ""), keywords)]
    log.info("ats: greenhouse/%s %d postings, %d title matches", slug, len(jobs), len(matched))

    roles: list[Role] = []
    for index, job in enumerate(matched):
        named = (job.get("location") or {}).get("name")
        location = _join_locations(
            [named] if named else [o.get("name") for o in job.get("offices") or [] if isinstance(o, Mapping)]
        )

        body = _strip_html(job.get("content", ""))
        if with_content and not body and index < max_detail:
            sleep(delay)
            body = _greenhouse_detail(slug, job.get("id"), get_json=get_json, timeout=timeout)

        roles.append(
            _make_role(
                company=company,
                title=str(job.get("title", "")).strip(),
                url=str(job.get("absolute_url", "")).strip(),
                location=location,
                board="greenhouse",
                # first_published is when the posting went live, which is the
                # signal that matters for "how long has this been out there".
                # updated_at only tells us the last edit.
                posted=_iso_date(job.get("first_published") or job.get("updated_at")),
                workplace=_greenhouse_location_type(job),
                body=body,
            )
        )
    return roles


def _greenhouse_location_type(job: Mapping) -> str:
    """Read Greenhouse's "Location Type" metadata field, when the board sets it.

    Values seen live: On-Site, Remote, Hybrid (Travel-Required). Reported as a
    workplace label only, never merged into the location.
    """
    for item in job.get("metadata") or []:
        if isinstance(item, Mapping) and str(item.get("name", "")).lower() == "location type":
            value = item.get("value")
            if value:
                return str(value).strip()
    return ""


def _greenhouse_detail(slug: str, job_id, *, get_json, timeout: float) -> str:
    """Fetch one Greenhouse posting's description. Empty string on any failure."""
    if not job_id:
        return ""
    url = f"https://boards-api.greenhouse.io/v1/boards/{urllib.parse.quote(slug)}/jobs/{job_id}"
    status, payload = get_json(url, timeout=timeout)
    if status != 200 or not isinstance(payload, dict):
        return ""
    return _strip_html(payload.get("content", ""))


def fetch_lever(
    company: str,
    slug: str,
    *,
    keywords: Sequence[str] = DEFAULT_TITLE_KEYWORDS,
    get_json: Callable[..., tuple[int, object]] = httpx_get_json,
    sleep: Callable[[float], None] = time.sleep,
    delay: float = DEFAULT_DELAY,
    timeout: float = DEFAULT_TIMEOUT,
    with_content: bool = True,
    max_detail: int = DEFAULT_MAX_DETAIL,
) -> list[Role]:
    """Fetch and normalise a Lever board.

    One request returns everything including plain-text descriptions, so there
    is no detail call and ``with_content`` only controls whether the description
    is kept.

    Location: ``categories.allLocations`` is the complete list for a posting and
    is preferred; ``categories.location`` is the single-location fallback. The
    bare ``country`` code is deliberately not used as a location, because "US"
    is not what the posting says and expanding it would be a guess.
    """
    url = f"https://api.lever.co/v0/postings/{urllib.parse.quote(slug)}?mode=json"
    status, payload = get_json(url, timeout=timeout)
    if status != 200 or not isinstance(payload, list):
        log.warning("ats: lever/%s returned %s", slug, status)
        return []

    matched = [j for j in payload if isinstance(j, dict) and title_matches(j.get("text", ""), keywords)]
    log.info("ats: lever/%s %d postings, %d title matches", slug, len(payload), len(matched))

    roles: list[Role] = []
    for job in matched:
        categories = job.get("categories") or {}
        locations = categories.get("allLocations") or [categories.get("location")]
        body = _clean_text(job.get("descriptionPlain", "")) if with_content else ""
        roles.append(
            _make_role(
                company=company,
                title=str(job.get("text", "")).strip(),
                url=str(job.get("hostedUrl") or job.get("applyUrl") or "").strip(),
                location=_join_locations(locations),
                board="lever",
                posted=_iso_date(job.get("createdAt")),
                workplace=str(job.get("workplaceType") or "").strip(),
                body=body,
            )
        )
    return roles


def fetch_ashby(
    company: str,
    slug: str,
    *,
    keywords: Sequence[str] = DEFAULT_TITLE_KEYWORDS,
    get_json: Callable[..., tuple[int, object]] = httpx_get_json,
    sleep: Callable[[float], None] = time.sleep,
    delay: float = DEFAULT_DELAY,
    timeout: float = DEFAULT_TIMEOUT,
    with_content: bool = True,
    max_detail: int = DEFAULT_MAX_DETAIL,
) -> list[Role]:
    """Fetch and normalise an Ashby job board.

    One request returns everything including ``descriptionPlain``.

    Location, and this is the trap on this board: Ashby exposes both a
    ``location`` string and an ``isRemote`` boolean, and they disagree
    constantly. Cognition's "Deployed Engineer - Europe" is ``isRemote: true``,
    ``workplaceType: "Hybrid"``, ``location: "London"``. Treating isRemote as a
    location would push a London role straight through a remote-only gate. So
    the location is built from ``location`` plus ``secondaryLocations`` only,
    and the remote flag is surfaced as a workplace label in the snippet where a
    human can weigh it.

    Only listed postings are returned: ``isListed: false`` means the company has
    pulled it from its public board.
    """
    url = f"https://api.ashbyhq.com/posting-api/job-board/{urllib.parse.quote(slug)}"
    status, payload = get_json(url, timeout=timeout)
    if status != 200 or not isinstance(payload, dict):
        log.warning("ats: ashby/%s returned %s", slug, status)
        return []

    jobs = [j for j in payload.get("jobs") or [] if isinstance(j, dict) and j.get("isListed", True)]
    matched = [j for j in jobs if title_matches(j.get("title", ""), keywords)]
    log.info("ats: ashby/%s %d postings, %d title matches", slug, len(jobs), len(matched))

    roles: list[Role] = []
    for job in matched:
        locations = [job.get("location")]
        locations += [s.get("location") for s in job.get("secondaryLocations") or [] if isinstance(s, Mapping)]
        body = _clean_text(job.get("descriptionPlain", "")) if with_content else ""
        if with_content and not body:
            body = _strip_html(job.get("descriptionHtml", ""))
        roles.append(
            _make_role(
                company=company,
                title=str(job.get("title", "")).strip(),
                url=str(job.get("jobUrl") or job.get("applyUrl") or "").strip(),
                location=_join_locations(locations),
                board="ashby",
                posted=_iso_date(job.get("publishedAt")),
                workplace=_ashby_workplace(job),
                body=body,
            )
        )
    return roles


def _ashby_workplace(job: Mapping) -> str:
    """Combine Ashby's workplaceType and isRemote into one honest label.

    Both are reported because they routinely disagree, and the disagreement is
    itself the signal worth reading.
    """
    parts = [str(job.get("workplaceType") or "").strip()]
    if job.get("isRemote"):
        parts.append("remote-flagged")
    return ", ".join(p for p in parts if p)


def fetch_smartrecruiters(
    company: str,
    slug: str,
    *,
    keywords: Sequence[str] = DEFAULT_TITLE_KEYWORDS,
    get_json: Callable[..., tuple[int, object]] = httpx_get_json,
    sleep: Callable[[float], None] = time.sleep,
    delay: float = DEFAULT_DELAY,
    timeout: float = DEFAULT_TIMEOUT,
    with_content: bool = True,
    max_detail: int = DEFAULT_MAX_DETAIL,
) -> list[Role]:
    """Fetch and normalise a SmartRecruiters board.

    This is the only board here that pages (100 per page, ``totalFound`` gives
    the total) and the only one whose list response carries neither a public URL
    nor a description. Both come from the per-posting detail call, which is made
    for matched postings only. ``postingUrl`` is read from that response rather
    than assembled from a URL template, so the link is the company's own and not
    a pattern that could silently rot.

    Location: ``location.fullLocation`` is the faithful string. Its empty middle
    segment ("Beijing, , China") is tidied, nothing else is rewritten. The
    ``remote`` and ``hybrid`` booleans become a workplace label, never a location.
    """
    quoted = urllib.parse.quote(slug)
    postings: list[dict] = []
    offset = 0
    for _ in range(_SR_MAX_PAGES):
        url = (
            f"https://api.smartrecruiters.com/v1/companies/{quoted}/postings"
            f"?limit={_SR_PAGE_SIZE}&offset={offset}"
        )
        status, payload = get_json(url, timeout=timeout)
        if status != 200 or not isinstance(payload, dict):
            if offset == 0:
                log.warning("ats: smartrecruiters/%s returned %s", slug, status)
            break
        page = [p for p in payload.get("content") or [] if isinstance(p, dict)]
        postings.extend(page)
        offset += len(page)
        if not page or offset >= int(payload.get("totalFound") or 0):
            break
        sleep(delay)

    matched = [p for p in postings if title_matches(p.get("name", ""), keywords)]
    log.info("ats: smartrecruiters/%s %d postings, %d title matches", slug, len(postings), len(matched))

    roles: list[Role] = []
    for index, posting in enumerate(matched):
        # WHY the detail call is not optional here, unlike Greenhouse: the list
        # response carries no public URL at all, so this is the only way to get
        # a real link. max_detail therefore truncates the result rather than
        # merely dropping descriptions, and it says so out loud. A silent drop
        # here would look exactly like "that company is not hiring".
        if index >= max_detail:
            log.warning(
                "ats: smartrecruiters/%s truncated at max_detail=%d, %d match(es) not fetched",
                slug, max_detail, len(matched) - index,
            )
            break

        location = posting.get("location") or {}
        sleep(delay)
        detail = _smartrecruiters_detail(slug, posting.get("id"), get_json=get_json, timeout=timeout)

        url = str(detail.get("postingUrl") or detail.get("applyUrl") or "").strip()
        if not url:
            # No verified public link means no role. Emitting a guessed URL would
            # hand the verify stage a link that may 404 or point at the wrong job.
            log.warning("ats: smartrecruiters/%s no public url for %r", slug, posting.get("name"))
            continue

        body = _smartrecruiters_body(detail) if with_content else ""
        roles.append(
            _make_role(
                company=company,
                title=str(posting.get("name", "")).strip(),
                url=url,
                location=_smartrecruiters_location(location),
                board="smartrecruiters",
                posted=_iso_date(posting.get("releasedDate")),
                workplace=_smartrecruiters_workplace(location),
                body=body,
            )
        )
    return roles


def _smartrecruiters_location(location: Mapping) -> str:
    """Best available location string for a SmartRecruiters posting.

    ``fullLocation`` is the board's own rendered string and is preferred whole.
    Only when it is absent are city and country assembled, and the country is a
    two-letter code that is upper-cased but never expanded to a country name,
    because expanding it would be inference rather than extraction.
    """
    full = location.get("fullLocation")
    if full:
        return _join_locations([full])
    country = str(location.get("country") or "").strip().upper()
    return _join_locations([", ".join(p for p in (location.get("city"), country) if p)])


def _smartrecruiters_workplace(location: Mapping) -> str:
    labels = [name for name in ("remote", "hybrid") if location.get(name)]
    return ", ".join(labels)


def _smartrecruiters_detail(slug: str, posting_id, *, get_json, timeout: float) -> dict:
    """Fetch one SmartRecruiters posting. Empty dict on any failure."""
    if not posting_id:
        return {}
    url = (
        f"https://api.smartrecruiters.com/v1/companies/{urllib.parse.quote(slug)}"
        f"/postings/{urllib.parse.quote(str(posting_id))}"
    )
    status, payload = get_json(url, timeout=timeout)
    return payload if status == 200 and isinstance(payload, dict) else {}


def _smartrecruiters_body(detail: Mapping) -> str:
    """Flatten the jobAd sections that describe the role itself.

    companyDescription is skipped: it is identical on every posting at a company
    and would crowd the useful text out of a bounded snippet.
    """
    sections = (detail.get("jobAd") or {}).get("sections") or {}
    parts = []
    for key in ("jobDescription", "qualifications"):
        text = (sections.get(key) or {}).get("text")
        if text:
            parts.append(_strip_html(text))
    return " ".join(parts).strip()


def _clean_text(raw: str) -> str:
    """Collapse whitespace in an already-plain-text description."""
    return _WS_RE.sub(" ", html.unescape(raw or "")).strip()


FETCHERS: dict[str, Callable[..., list[Role]]] = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "smartrecruiters": fetch_smartrecruiters,
}


# ---------------------------------------------------------------------------
# Top level
# ---------------------------------------------------------------------------


def _dedupe(roles: list[Role]) -> list[Role]:
    """Drop duplicate roles (same id) within a batch, keeping the first seen.

    Matches discover._dedupe. It matters more here, because one company can be
    listed under two boards during an ATS migration and both will still serve
    the same posting.
    """
    seen: set[str] = set()
    out: list[Role] = []
    for role in roles:
        if role.id not in seen:
            seen.add(role.id)
            out.append(role)
    return out


def fetch_ats(
    registry: Iterable,
    *,
    keywords: Sequence[str] = DEFAULT_TITLE_KEYWORDS,
    get_json: Callable[..., tuple[int, object]] = httpx_get_json,
    sleep: Callable[[float], None] = time.sleep,
    delay: float = DEFAULT_DELAY,
    timeout: float = DEFAULT_TIMEOUT,
    with_content: bool = True,
    max_detail: int = DEFAULT_MAX_DETAIL,
) -> list[Role]:
    """Enumerate every board in the registry and return deduplicated Roles.

    Args:
        registry: Board objects, or mappings with company/board/slug keys as
            they would come out of a TOML config.
        keywords: title keywords to match on. Configurable so the target role
            set can move without editing this module.
        get_json: callable(url, timeout=...) -> (status, parsed). Injected so
            the whole stage runs offline in tests.
        sleep: callable(seconds). Injected so tests do not actually wait.
        delay: seconds between calls, to stay polite to shared public endpoints.
        timeout: per-request timeout in seconds.
        with_content: fetch role descriptions. Off makes a run much cheaper at
            the cost of the snippet the score stage reads.
        max_detail: per-company cap on description/detail requests.

    Returns:
        Roles from every board that answered, deduplicated by role id. Empty
        list if every board failed. Never raises: a bad registry row, a dead
        board, or a renamed slug is logged and skipped, so one broken company
        can never cost Alex the rest of the week's discoveries.
    """
    roles: list[Role] = []
    for entry in registry:
        try:
            board = _coerce(entry)
        except (ValueError, AttributeError, TypeError) as exc:
            log.warning("ats: skipping registry entry: %s", exc)
            continue

        fetcher = FETCHERS[board.board]
        try:
            found = fetcher(
                board.company,
                board.slug,
                keywords=keywords,
                get_json=get_json,
                sleep=sleep,
                delay=delay,
                timeout=timeout,
                with_content=with_content,
                max_detail=max_detail,
            )
        except Exception as exc:  # noqa: BLE001 - partial results by design
            log.warning("ats: %s/%s failed: %s", board.board, board.slug, exc)
            continue

        roles.extend(found)
        sleep(delay)

    deduped = _dedupe(roles)
    log.info("ats: %d role(s) after dedupe", len(deduped))
    return deduped


def verify_slug(
    board: str,
    slug: str,
    *,
    get_json: Callable[..., tuple[int, object]] = httpx_get_json,
    timeout: float = DEFAULT_TIMEOUT,
) -> tuple[bool, int, str]:
    """Check a slug before it goes into a registry.

    Returns ``(ok, posting_count, note)``. A slug is only ok when the endpoint
    answers 200 in the shape that board is supposed to return. A count of zero
    is reported as not-ok with a note, because a board that answers 200 with an
    empty list is indistinguishable from a wrong slug and is exactly how a
    registry silently fills up with companies that look like they are not
    hiring. Verify before committing: a registry full of 404s is worse than a
    short one.
    """
    if board not in FETCHERS:
        return False, 0, f"unsupported board {board!r}"

    if board == "greenhouse":
        url = f"https://boards-api.greenhouse.io/v1/boards/{urllib.parse.quote(slug)}/jobs"
    elif board == "lever":
        url = f"https://api.lever.co/v0/postings/{urllib.parse.quote(slug)}?mode=json"
    elif board == "ashby":
        url = f"https://api.ashbyhq.com/posting-api/job-board/{urllib.parse.quote(slug)}"
    else:
        url = f"https://api.smartrecruiters.com/v1/companies/{urllib.parse.quote(slug)}/postings?limit=1"

    status, payload = get_json(url, timeout=timeout)
    if status != 200:
        return False, 0, f"HTTP {status}"
    if isinstance(payload, list):
        count = len(payload)
    elif isinstance(payload, dict):
        count = int(payload.get("totalFound") or len(payload.get("jobs") or []))
    else:
        return False, 0, "unparseable body"
    if count == 0:
        return False, 0, "200 but zero postings"
    return True, count, "ok"
