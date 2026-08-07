"""Runner: orchestrate the Scout stages into one weekly run.

Sequence: start_run, discover (ATS and/or LLM), merge, verify, filter_dedupe
(against seen ids), remote gate, score, contest proxy, draft (HIGH only),
track.upsert, stale_applications, compose + deliver digest, finish_run. Every
stage is wrapped in its own try/except so a single failure is logged to the audit
table, appended to RunResult.errors, and the run continues with whatever partial
results it has. Nothing here is hard-wired: the LLM client, the fetcher, and the
write/notify side effects are all injected, so the whole loop runs offline in tests.

Two discovery sources feed the same pipeline:

    ats  scout.ats reads company-owned applicant tracking boards (Greenhouse,
         Lever, Ashby, SmartRecruiters) over their public JSON endpoints. No LLM,
         no API key, deterministic. A posting appears on its company's own board
         before any aggregator indexes it, so this is the source of the
         low-application-volume roles that are the whole point of the exercise.
    llm  scout.discover asks an LLM with web access. Broad, but it can only find
         what a search engine has already indexed, which is the same pool Seek
         and LinkedIn aggregate, and by then the role is high-volume.

The two are merged and deduplicated before verify, so every stage after discovery
is source-agnostic. The ATS path needs no LLM at all, so it survives an LLM outage
and is cheap enough to schedule on its own: see `sources` below.

Two signals stages sit between filtering and the digest:

    remote gate    signals.remote_gate reads the posting body for residency,
                   work-authorisation and timezone clauses that substring
                   location matching cannot see.
    contest proxy  signals.rank_by_contest attaches a transparent 0-100 proxy for
                   how under-contested a role probably is. Higher means quieter.
                   It is a proxy, never an applicant count, and the digest says so.

Safety: with dry_run=True the runner discovers, verifies, filters, and scores but
never drafts, never writes to the database, and never delivers a digest. The only
autonomous output of a live run is the weekly digest, delivered only to Alex.
"""
from __future__ import annotations

import logging
import os
import tomllib
import uuid
from datetime import datetime, timezone
from pathlib import Path

from scout import discover as discover_stage
from scout import draft as draft_stage
from scout import digest as digest_stage
from scout import state
from scout import track
from scout.filter_dedupe import filter_roles
from scout.models import Role, RunResult, Score
from scout.score import score_role

log = logging.getLogger("scout.runner")

# Discovery sources the runner knows how to drive.
_ALL_SOURCES = ("ats", "llm")

# How many roles the least-contested block lists before it summarises the rest.
_CONTEST_BLOCK_LIMIT = 12


def _new_run_id() -> str:
    """Short, unique run id for the runs and audit tables."""
    return uuid.uuid4().hex[:16]


def resolve_sources(config: dict, override: str | None = None) -> set[str]:
    """Decide which discovery sources run this pass.

    Precedence: the explicit `sources` argument, then the SCOUT_SOURCES environment
    variable, then config [sources], then both. Accepts "all", "ats", "llm", or a
    comma-separated pair.

    WHY the environment variable: the deterministic ATS-only pass is the cheap one
    worth running on a schedule, and a launchd plist or a shell one-liner can set an
    environment variable without maintaining a second full copy of the private
    config, which would drift.
    """
    raw = (override or os.environ.get("SCOUT_SOURCES") or "").strip().lower()
    if raw:
        if raw in {"all", "both", "*"}:
            return set(_ALL_SOURCES)
        picked = {part.strip() for part in raw.split(",") if part.strip()}
        picked &= set(_ALL_SOURCES)
        # An unrecognised value is a typo, not an instruction to discover nothing.
        return picked or set(_ALL_SOURCES)

    cfg = config.get("sources", {})
    if not isinstance(cfg, dict) or not cfg:
        return set(_ALL_SOURCES)
    return {name for name in _ALL_SOURCES if cfg.get(name, True)} or set(_ALL_SOURCES)


# --- discovery: ATS ---------------------------------------------------------------

def _registry_path(raw: str) -> Path:
    """Resolve a configured registry path against the repo root.

    WHY not just trust the string: Scout is run from launchd, from a shell, and from
    tests, so the working directory is not dependable. A relative registry path is
    resolved against the package's own parent so it means the same thing every time.
    """
    path = Path(os.path.expanduser(raw))
    if path.is_absolute():
        return path
    return Path(__file__).resolve().parent.parent / path


def load_ats_registry(config: dict) -> list[dict]:
    """Build the list of boards to poll, from the registry file plus inline rows.

    The shared registry lives in its own TOML file ([ats] registry_file) as [[company]]
    rows, because it is a long, heavily annotated list of verified slugs that deserves
    its own file and its own review history. [ats] companies stays available for
    additions that are not in the shared file yet.

    Rows in the shared file key the display name as ``name``; ats.Board.from_mapping
    expects ``company``. Normalising here is the runner's job as the integration layer,
    and it means a row missing either spelling is skipped rather than silently renaming
    a company to the empty string. Deduplicated on (board, slug), so listing a company
    in both places polls it once.
    """
    rows: list[dict] = []
    raw_path = config.get("ats", {}).get("registry_file")
    if raw_path:
        path = _registry_path(str(raw_path))
        # A missing or malformed registry file must not abort discovery: the inline
        # rows below are still usable, and the caller logs whatever came back.
        try:
            with open(path, "rb") as fh:
                rows.extend(tomllib.load(fh).get("company") or [])
        except (OSError, tomllib.TOMLDecodeError) as exc:
            log.warning("ats: registry file %s unusable: %s", path, exc)

    rows.extend(config.get("ats", {}).get("companies") or [])

    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        company = str(row.get("company") or row.get("name") or "").strip()
        board = str(row.get("board") or "").strip().lower()
        slug = str(row.get("slug") or "").strip()
        if not company or not board or not slug:
            log.warning("ats: skipping incomplete registry row: %r", row)
            continue
        key = (board, slug)
        if key in seen:
            continue
        seen.add(key)
        out.append({"company": company, "board": board, "slug": slug})
    return out


def ats_discover(*, config: dict, get_json=None) -> list[Role]:
    """Read every board in the configured registry and return Role candidates.

    ``get_json`` is injected so an offline run can stub the network. Left as None it
    uses the ats module's own httpx client. ats.fetch_ats never raises: a dead board
    or a renamed slug is logged and skipped, so this returns whatever answered.
    """
    from scout import ats as ats_mod

    ats_cfg = config.get("ats", {})
    registry = load_ats_registry(config)
    if not registry:
        return []

    keywords = ats_cfg.get("title_keywords") or list(ats_mod.DEFAULT_TITLE_KEYWORDS)
    kwargs = {
        "keywords": keywords,
        "delay": float(ats_cfg.get("delay", ats_mod.DEFAULT_DELAY)),
        "timeout": float(ats_cfg.get("timeout", ats_mod.DEFAULT_TIMEOUT)),
        "with_content": bool(ats_cfg.get("with_content", True)),
        "max_detail": int(ats_cfg.get("max_detail", ats_mod.DEFAULT_MAX_DETAIL)),
    }
    if get_json is not None:
        kwargs["get_json"] = get_json
    return ats_mod.fetch_ats(registry, **kwargs)


def _merge_sources(*batches: list[Role]) -> list[Role]:
    """Concatenate candidate batches and drop duplicate role ids, first wins.

    ATS batches are passed first because a company's own board is the authoritative
    record of a posting: a canonical URL, the real location string, and the posting
    date, rather than an aggregator listing or an LLM paraphrase of one.
    """
    seen: set[str] = set()
    out: list[Role] = []
    for batch in batches:
        for role in batch or []:
            if role.id in seen:
                continue
            seen.add(role.id)
            out.append(role)
    return out


# --- signals: remote gate and contest proxy ---------------------------------------

def _descriptions(roles: list[Role]) -> dict[str, str]:
    """Map role id to whatever body text discovery captured.

    The ATS stage packs the posting body into Role.snippet, which is exactly what the
    remote gate needs to find residency and work-authorisation clauses. Passing it in
    is the difference between gating on a location string and gating on the posting.
    """
    return {r.id: r.snippet for r in roles if r.snippet}


def remote_gate(roles: list[Role], *, config: dict):
    """Run the strict remote gate. Returns (kept, rejected).

    filter_dedupe already did substring location matching. This is the second, harder
    pass: it reads the posting text for the US-only and residency clauses that a
    location field reading "Remote" hides. Conservative by design, it BLOCKs only on
    high-confidence evidence, so a terse posting is kept for review rather than
    silently deleted.
    """
    from scout import signals as signals_mod

    allow_review = bool(config.get("signals", {}).get("allow_review", True))
    return signals_mod.remote_gate(
        roles,
        filters=config.get("filters", {}),
        descriptions=_descriptions(roles),
        allow_review=allow_review,
    )


def _ages(roles: list[Role], *, now: str) -> dict[str, float]:
    """Days since each posting went up, keyed by role id.

    Freshness is a weighted component of the contest proxy and the ATS stage already
    knows the posting date, so deriving it here costs nothing and no network call. A
    role with no parseable date is simply absent, which leaves freshness unmeasured
    rather than guessed.
    """
    try:
        ref = datetime.fromisoformat(now.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        ref = datetime.now(timezone.utc)
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=timezone.utc)

    out: dict[str, float] = {}
    for role in roles:
        posted = getattr(role, "posted_at", "")
        if not posted:
            continue
        try:
            when = datetime.fromisoformat(str(posted).replace("Z", "+00:00"))
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        age = (ref - when).total_seconds() / 86400.0
        if age >= 0:
            out[role.id] = age
    return out


def rank_contest(roles: list[Role], *, config: dict, now: str):
    """Attach the low-contest proxy to each role, most-quiet first.

    The aggregator probe is the heaviest-weighted component and the only network call
    in the signals module, so it is opt-in via [signals] aggregator_probe. Off, the
    proxy still works from freshness, company obscurity, title specificity and
    location narrowness, and flags itself as thinner.
    """
    from scout import signals as signals_mod

    sig_cfg = config.get("signals", {})
    probe = (
        signals_mod.duckduckgo_aggregator_probe
        if sig_cfg.get("aggregator_probe", False)
        else None
    )
    return signals_mod.rank_by_contest(roles, probe=probe, ages=_ages(roles, now=now))


def _contest_of(role: Role):
    """The ContestSignal signals attached to this role, or None."""
    signal = getattr(role, "contest", None)
    return signal if getattr(signal, "proxy_score", None) is not None else None


def least_contested_section(ranked, *, limit: int = _CONTEST_BLOCK_LIMIT) -> list[str]:
    """Build the digest block that answers "what has nobody else found yet".

    Alex's weekly question is not "what scores highest", it is "what should I apply to
    that others have not found". That is a different ordering from the fit score, so
    it gets its own block at the top, ordered by the contest proxy rather than by
    HIGH/MED. Every line says proxy, because it is a proxy and never an applicant
    count. Roles the proxy could not measure are omitted rather than ranked on a
    guess. Returns [] when there is nothing to say, so the caller can skip the block.

    ``ranked`` is the (Role, ContestSignal) list from signals.rank_by_contest, which
    is already sorted most-quiet first.
    """
    scored = [(r, s) for r, s in ranked if s is not None and s.proxy_score is not None]
    if not scored:
        return []
    lines = [
        f"Least contested first ({len(scored)})",
        "----------------------",
        "Proxy for how crowded a role is, not an applicant count. Higher is quieter.",
        "",
    ]
    for role, signal in scored[:limit]:
        fit = role.score.label if role.score else "UNSCORED"
        lines.append(
            f"- [{signal.proxy_score}/100 {signal.band}] {role.company}: {role.title} "
            f"(fit {fit})\n"
            f"  {role.location} | {signal.reason}\n"
            f"  {role.url}"
        )
    if len(scored) > limit:
        lines.append(f"  ... and {len(scored) - limit} more in the sections below.")
    lines.append("")
    return lines


def _insert_section(digest_text: str, section: list[str]) -> str:
    """Splice the least-contested block in above the HIGH priority block.

    WHY splice rather than compose: digest.compose_digest owns the digest layout and
    stays unchanged, so its tests keep passing and this stage adds a section without
    taking ownership of the format. If the anchor is not found the section is
    appended instead, so it is never silently lost.
    """
    if not section:
        return digest_text
    lines = digest_text.splitlines()
    anchor = next(
        (i for i, line in enumerate(lines) if line.startswith("HIGH priority")), None
    )
    if anchor is None:
        return "\n".join(lines + [""] + section)
    return "\n".join(lines[:anchor] + section + lines[anchor:])


# --- the loop ---------------------------------------------------------------------

def run(
    *,
    config: dict,
    conn,
    client,
    now: str,
    fetch,
    write,
    notify,
    dry_run: bool = False,
    sources: str | None = None,
    ats_get_json=None,
) -> RunResult:
    """Run the full Scout loop once.

    Args:
        config: merged config dict (sources, search, ats, filters, signals, scoring,
            profile, notify, paths).
        conn: open sqlite connection (the scout.db source of truth).
        client: an LLMClient (real ClaudeCliClient live, FakeLLMClient in tests).
            Unused when the LLM source is switched off.
        now: ISO timestamp string used for every stage that needs the clock.
        fetch: callable(url) -> (status_code, text) for the verify liveness check.
        write: callable(path, text) for digest delivery (injected for tests).
        notify: callable(text) for ntfy delivery (injected for tests).
        dry_run: when True, discover/verify/filter/score only. No drafts, no db
            writes, no digest delivery, no heartbeat.
        sources: "all", "ats", "llm", or a comma-separated pair. Overrides the
            SCOUT_SOURCES environment variable and config [sources].
        ats_get_json: callable(url, timeout=...) -> (status, parsed) for the ATS
            boards. Injected so an offline run can stub every board call. None uses
            the ats module's real httpx client.

    Returns:
        RunResult with per-stage counts and any stage errors.
    """
    run_id = _new_run_id()
    result = RunResult(run_id=run_id)

    search = config.get("search", {})
    ats_cfg = config.get("ats", {})
    filters = config.get("filters", {})
    scoring = config.get("scoring", {})
    profile = config.get("profile", {})
    notify_cfg = config.get("notify", {})
    paths = config.get("paths", {})

    active = resolve_sources(config, sources)

    state.start_run(conn, run_id, now)
    state.record_audit(conn, run_id, "sources", "selected", detail=",".join(sorted(active)))

    # 1a. Discover (ATS): read company boards directly. No LLM, no API key. This is
    #     the path that finds a posting before the aggregators index it.
    ats_candidates: list[Role] = []
    if "ats" in active:
        try:
            registry_size = len(load_ats_registry(config))
            ats_candidates = ats_discover(config=config, get_json=ats_get_json)
            state.record_audit(
                conn, run_id, "ats", "found",
                detail=f"{len(ats_candidates)} candidate(s) from {registry_size} board(s)",
            )
            # fetch_ats swallows per-board failures by design, so a totally dead
            # network looks identical to a quiet week. Flag the shape of it in the
            # audit trail: a non-empty registry returning nothing is worth a look.
            if registry_size and not ats_candidates:
                state.record_audit(
                    conn, run_id, "ats", "empty",
                    detail=f"{registry_size} board(s) configured, zero roles matched. "
                           "Check connectivity and title_keywords.",
                )
        except Exception as exc:  # noqa: BLE001 - partial results by design
            result.errors.append(f"ats: {exc}")
            state.record_audit(conn, run_id, "ats", "error", detail=str(exc))

    # 1b. Discover (LLM): ask the LLM (web enabled) for live role candidates.
    llm_candidates: list[Role] = []
    if "llm" in active:
        try:
            llm_candidates = discover_stage.discover(client, search=search)
            state.record_audit(
                conn, run_id, "discover", "found",
                detail=f"{len(llm_candidates)} candidate(s)",
            )
        except Exception as exc:  # noqa: BLE001 - partial results by design
            result.errors.append(f"discover: {exc}")
            state.record_audit(conn, run_id, "discover", "error", detail=str(exc))

    # 1c. Merge both sources into one list, deduplicated by role id, so everything
    #     downstream is source-agnostic and a role found twice is drafted once.
    candidates = _merge_sources(ats_candidates, llm_candidates)
    result.discovered = len(candidates)
    result.ats_discovered = len(ats_candidates)  # type: ignore[attr-defined]
    result.llm_discovered = len(llm_candidates)  # type: ignore[attr-defined]
    if ats_candidates and llm_candidates:
        overlap = len(ats_candidates) + len(llm_candidates) - len(candidates)
        state.record_audit(
            conn, run_id, "merge", "deduped",
            detail=f"ats {len(ats_candidates)} + llm {len(llm_candidates)} "
                   f"= {len(candidates)} unique ({overlap} overlap)",
        )

    # 2. Verify: drop dead links, closed postings, and wrong locations.
    #    ATS candidates skip the HTTP liveness check by default. The board API only
    #    returns postings that are currently open, so re-fetching every apply URL is a
    #    slow second opinion on a fact the API already established, and several boards
    #    render the posting in JavaScript, so the closed-marker scan on the HTML body
    #    is unreliable there and risks dropping live roles. ATS roles are still
    #    location-filtered by the filter stage and the remote gate below. Set
    #    ats.verify_http = true to force the HTTP check on them anyway.
    verified: list[Role] = candidates
    try:
        if ats_candidates and not ats_cfg.get("verify_http", False):
            ats_ids = {r.id for r in ats_candidates}
            trusted = [r for r in candidates if r.id in ats_ids]
            to_check = [r for r in candidates if r.id not in ats_ids]
            verified = trusted + verify_roles(to_check, fetch=fetch, filters=filters)
        else:
            verified = verify_roles(candidates, fetch=fetch, filters=filters)
        result.verified = len(verified)
        state.record_audit(
            conn, run_id, "verify", "live",
            detail=f"{len(verified)} live of {len(candidates)}",
        )
    except Exception as exc:  # noqa: BLE001
        result.errors.append(f"verify: {exc}")
        state.record_audit(conn, run_id, "verify", "error", detail=str(exc))

    # 3. Filter + dedupe: drop already-seen ids, in-batch dupes, and constraint
    #    violations the verify stage did not already catch.
    qualified: list[Role] = verified
    try:
        seen = state.seen_ids(conn)
        qualified = filter_roles(verified, seen_ids=seen, filters=filters)
        result.new_qualified = len(qualified)
        state.record_audit(
            conn, run_id, "filter", "qualified",
            detail=f"{len(qualified)} new qualified",
        )
    except Exception as exc:  # noqa: BLE001
        result.errors.append(f"filter: {exc}")
        state.record_audit(conn, run_id, "filter", "error", detail=str(exc))

    # 3b. Remote gate: read the posting text, not just the location field. This is
    #     what catches "Remote" postings whose body says US residency required.
    if filters.get("remote_only") and qualified:
        try:
            before = len(qualified)
            qualified, rejected = remote_gate(qualified, config=config)
            result.new_qualified = len(qualified)
            result.remote_blocked = len(rejected)  # type: ignore[attr-defined]
            state.record_audit(
                conn, run_id, "remote_gate", "gated",
                detail=f"{len(qualified)} kept of {before}, {len(rejected)} blocked",
            )
            for role, verdict in rejected:
                state.record_audit(
                    conn, run_id, "remote_gate", "blocked", role_id=role.id,
                    detail=f"{role.company} {role.title}: {verdict.reason}",
                )
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"remote_gate: {exc}")
            state.record_audit(conn, run_id, "remote_gate", "error", detail=str(exc))

    # 4. Score: deterministic HIGH/MED/LOW with a one-line reason.
    high: list[Role] = []
    med: list[Role] = []
    try:
        for role in qualified:
            score_role(role, scoring=scoring)
            if role.score is Score.HIGH:
                high.append(role)
            elif role.score is Score.MED:
                med.append(role)
        result.high = len(high)
        state.record_audit(
            conn, run_id, "score", "scored",
            detail=f"{len(high)} HIGH, {len(med)} MED",
        )
    except Exception as exc:  # noqa: BLE001
        result.errors.append(f"score: {exc}")
        state.record_audit(conn, run_id, "score", "error", detail=str(exc))

    # 4b. Contest proxy: rank the shortlist by how uncontested it probably is. Run on
    #     HIGH plus MED only, because that is what the digest shows and the optional
    #     aggregator probe costs a throttled network call per role.
    ranked: list = []
    try:
        shortlist = high + med
        if shortlist:
            ranked = rank_contest(shortlist, config=config, now=now)
            measured = sum(1 for _, s in ranked if s.proxy_score is not None)
            result.contest_scored = measured  # type: ignore[attr-defined]
            state.record_audit(
                conn, run_id, "signals", "contest",
                detail=f"{measured} of {len(shortlist)} roles carry a proxy score",
            )
    except Exception as exc:  # noqa: BLE001
        result.errors.append(f"signals: {exc}")
        state.record_audit(conn, run_id, "signals", "error", detail=str(exc))

    # 5. Draft: HIGH roles only, self-critique gated. Skipped entirely in dry-run.
    if not dry_run:
        cv_text = _read_cv_text(paths)
        for role in high:
            try:
                draft_stage.draft_role(client, role, profile=profile, cv_text=cv_text)
                if role.drafts and "_flag" not in role.drafts:
                    result.drafted += 1
                state.record_audit(
                    conn, run_id, "draft", "drafted", role_id=role.id,
                    detail=f"{role.company} {role.title}",
                )
            except Exception as exc:  # noqa: BLE001
                result.errors.append(f"draft ({role.company}): {exc}")
                state.record_audit(
                    conn, run_id, "draft", "error", role_id=role.id, detail=str(exc),
                )

    # 6. Persist: upsert qualified roles. Skipped in dry-run (writes nothing).
    if not dry_run:
        try:
            track.upsert_roles(conn, qualified, now=now)
            state.record_audit(
                conn, run_id, "track", "upserted",
                detail=f"{len(qualified)} role(s)",
            )
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"track: {exc}")
            state.record_audit(conn, run_id, "track", "error", detail=str(exc))

    # 7. Stale-application follow-ups. This stamps follow_up_due, so it is a write
    #    and is skipped in dry-run.
    stale: list[Role] = []
    if not dry_run:
        try:
            follow_up_days = int(notify_cfg.get("follow_up_days", 10))
            stale = track.stale_applications(conn, now=now, follow_up_days=follow_up_days)
            if stale:
                state.record_audit(
                    conn, run_id, "follow_up", "due",
                    detail=f"{len(stale)} follow-up(s)",
                )
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"follow_up: {exc}")
            state.record_audit(conn, run_id, "follow_up", "error", detail=str(exc))

    # 8. Compose the digest. Always composed (cheap, no side effects) so a dry-run
    #    can preview it. Delivery (file + ntfy) is skipped in dry-run.
    digest_text = ""
    try:
        digest_text = digest_stage.compose_digest(
            shortlist=high, med=med, stale=stale, run=result,
        )
        # Lead with least-contested, above HIGH priority, because "what has nobody
        # else found" is the question the week actually turns on.
        digest_text = _insert_section(digest_text, least_contested_section(ranked))
        # Attach the composed text as a runtime convenience attribute so the CLI
        # can print a preview. RunResult has no __slots__, so this is safe and
        # does not change the declared dataclass contract.
        result.digest = digest_text  # type: ignore[attr-defined]
        if not dry_run:
            digests_dir = paths.get("digests_dir", "digests")
            ntfy_topic = notify_cfg.get("ntfy_topic", "")
            path = digest_stage.deliver(
                digest_text,
                digests_dir=digests_dir,
                ntfy_topic=ntfy_topic,
                now=now,
                write=write,
                notify=notify,
            )
            state.record_audit(conn, run_id, "digest", "delivered", detail=path)
    except Exception as exc:  # noqa: BLE001
        result.errors.append(f"digest: {exc}")
        state.record_audit(conn, run_id, "digest", "error", detail=str(exc))

    # 9. Close the run out and write the heartbeat. The heartbeat is the signal the
    #    launchd wrapper checks, so it is skipped in dry-run (no file side effects).
    status = "ok" if not result.errors else "partial"
    counts = {
        "sources": ",".join(sorted(active)),
        "ats_discovered": getattr(result, "ats_discovered", 0),
        "llm_discovered": getattr(result, "llm_discovered", 0),
        "discovered": result.discovered,
        "verified": result.verified,
        "remote_blocked": getattr(result, "remote_blocked", 0),
        "new_qualified": result.new_qualified,
        "high": result.high,
        "contest_scored": getattr(result, "contest_scored", 0),
        "drafted": result.drafted,
        "errors": len(result.errors),
    }
    error_text = "; ".join(result.errors) if result.errors else None
    state.finish_run(conn, run_id, now, counts=counts, status=status, error=error_text)

    if not dry_run:
        heartbeat = paths.get("heartbeat")
        if heartbeat:
            try:
                state.write_heartbeat(heartbeat, run_id, now)
            except Exception as exc:  # noqa: BLE001
                result.errors.append(f"heartbeat: {exc}")

    return result


def verify_roles(roles, *, fetch, filters):
    """Thin wrapper so the verify import stays lazy and testable.

    The verify stage owns the liveness + location logic; this keeps the runner's
    import surface small and gives one place to evolve verify wiring later.
    """
    from scout.verify import verify

    return verify(roles, fetch=fetch, filters=filters)


def _read_cv_text(paths: dict) -> str:
    """Best-effort read of the CV as plain text for the draft stage.

    The configured CV is typically a PDF, which we do not parse here. A plain-text
    or markdown CV is read directly. Any failure returns an empty string so the
    draft stage simply has less context, never a crash.
    """
    cv = paths.get("cv")
    if not cv:
        return ""
    try:
        from pathlib import Path

        p = Path(cv)
        if p.suffix.lower() in {".txt", ".md"} and p.exists():
            return p.read_text(encoding="utf-8", errors="ignore")
    except Exception:  # noqa: BLE001
        return ""
    return ""
