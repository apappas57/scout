"""Runner: orchestrate the Scout stages into one weekly run.

Sequence: start_run, discover, verify, filter_dedupe (against seen ids), score,
draft (HIGH only), track.upsert, stale_applications, compose + deliver digest,
finish_run. Every stage is wrapped in its own try/except so a single failure is
logged to the audit table, appended to RunResult.errors, and the run continues
with whatever partial results it has. Nothing here is hard-wired: the LLM client,
the fetcher, and the write/notify side effects are all injected, so the whole
loop runs fully offline in tests.

Safety: with dry_run=True the runner discovers, verifies, filters, and scores but
never drafts, never writes to the database, and never delivers a digest. The only
autonomous output of a live run is the weekly digest, delivered only to Alex.
"""
from __future__ import annotations

import uuid

from scout import discover as discover_stage
from scout import draft as draft_stage
from scout import digest as digest_stage
from scout import state
from scout import track
from scout.filter_dedupe import filter_roles
from scout.models import Role, RunResult, Score
from scout.score import score_role


def _new_run_id() -> str:
    """Short, unique run id for the runs and audit tables."""
    return uuid.uuid4().hex[:16]


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
) -> RunResult:
    """Run the full Scout loop once.

    Args:
        config: merged config dict (search, filters, scoring, profile, notify, paths).
        conn: open sqlite connection (the scout.db source of truth).
        client: an LLMClient (real ClaudeCliClient live, FakeLLMClient in tests).
        now: ISO timestamp string used for every stage that needs the clock.
        fetch: callable(url) -> (status_code, text) for the verify liveness check.
        write: callable(path, text) for digest delivery (injected for tests).
        notify: callable(text) for ntfy delivery (injected for tests).
        dry_run: when True, discover/verify/filter/score only. No drafts, no db
            writes, no digest delivery, no heartbeat.

    Returns:
        RunResult with per-stage counts and any stage errors.
    """
    run_id = _new_run_id()
    result = RunResult(run_id=run_id)

    search = config.get("search", {})
    filters = config.get("filters", {})
    scoring = config.get("scoring", {})
    profile = config.get("profile", {})
    notify_cfg = config.get("notify", {})
    paths = config.get("paths", {})

    state.start_run(conn, run_id, now)

    # 1. Discover: ask the LLM (web enabled) for live role candidates.
    candidates: list[Role] = []
    try:
        candidates = discover_stage.discover(client, search=search)
        result.discovered = len(candidates)
        state.record_audit(
            conn, run_id, "discover", "found",
            detail=f"{len(candidates)} candidate(s)",
        )
    except Exception as exc:  # noqa: BLE001 - partial results by design
        result.errors.append(f"discover: {exc}")
        state.record_audit(conn, run_id, "discover", "error", detail=str(exc))

    # 2. Verify: drop dead links, closed postings, and wrong locations.
    verified: list[Role] = candidates
    try:
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
        "discovered": result.discovered,
        "verified": result.verified,
        "new_qualified": result.new_qualified,
        "high": result.high,
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
