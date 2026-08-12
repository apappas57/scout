from __future__ import annotations
import json
import sqlite3
from datetime import datetime, timedelta

from scout.models import Role, Score

_SCORE_BY_LABEL = {s.label: s for s in Score}


def _score_to_text(score) -> str | None:
    if score is None:
        return None
    if isinstance(score, Score):
        return score.label
    return str(score)


def _score_from_text(text) -> Score | None:
    if not text:
        return None
    return _SCORE_BY_LABEL.get(text)


def _row_to_role(row: sqlite3.Row) -> Role:
    drafts = {}
    raw = row["drafts_json"] if "drafts_json" in row.keys() else None
    if raw:
        try:
            drafts = json.loads(raw)
        except (ValueError, TypeError):
            drafts = {}
    return Role(
        company=row["company"],
        title=row["title"],
        url=row["url"],
        location=row["location"] or "",
        source=(row["source"] if "source" in row.keys() else "") or "",
        snippet=(row["snippet"] if "snippet" in row.keys() else "") or "",
        workplace=(row["workplace"] if "workplace" in row.keys() else "") or "",
        description=(row["description"] if "description" in row.keys() else "") or "",
        score=_score_from_text(row["score"] if "score" in row.keys() else None),
        score_reason=(row["score_reason"] if "score_reason" in row.keys() else "") or "",
        status=row["status"],
        drafts=drafts,
    )


def upsert_roles(conn: sqlite3.Connection, roles, *, now: str) -> None:
    """Insert new roles (first_seen=last_seen=now) or update last_seen, score, and
    drafts for roles already present. Idempotent on role id."""
    for role in roles:
        conn.execute(
            """
            INSERT INTO roles (
                id, company, title, url, location, source, snippet,
                workplace, description,
                score, score_reason, status, drafts_json,
                first_seen_at, last_seen_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                last_seen_at = excluded.last_seen_at,
                score = excluded.score,
                score_reason = excluded.score_reason,
                snippet = excluded.snippet,
                -- A re-sight fetched without content (with_content=false, or a role
                -- past the ats max_detail cap) arrives with empty workplace and
                -- description. Keep the stored signal rather than erasing it: the
                -- remote gate reads these fields, and #296 exists because a missing
                -- body hid Talkdesk's travel clause.
                workplace = COALESCE(NULLIF(excluded.workplace, ''), roles.workplace),
                description = COALESCE(NULLIF(excluded.description, ''), roles.description),
                drafts_json = excluded.drafts_json
            """,
            (
                role.id,
                role.company,
                role.title,
                role.url,
                role.location,
                role.source,
                role.snippet,
                role.workplace,
                role.description,
                _score_to_text(role.score),
                role.score_reason,
                role.status,
                json.dumps(role.drafts) if role.drafts else None,
                now,
                now,
            ),
        )
    conn.commit()


def stale_applications(conn: sqlite3.Connection, *, now: str, follow_up_days: int):
    """Return applied roles whose applied_at is older than follow_up_days and which
    have not yet had a follow_up_due stamped. Stamps follow_up_due so each stale
    application is surfaced once only."""
    cutoff = datetime.fromisoformat(now) - timedelta(days=follow_up_days)
    rows = conn.execute(
        "SELECT * FROM roles WHERE status = 'applied' "
        "AND applied_at IS NOT NULL AND follow_up_due IS NULL"
    ).fetchall()

    stale = []
    for row in rows:
        try:
            applied_at = datetime.fromisoformat(row["applied_at"])
        except (ValueError, TypeError):
            continue
        if applied_at <= cutoff:
            stale.append(row)

    for row in stale:
        conn.execute(
            "UPDATE roles SET follow_up_due = ? WHERE id = ?", (now, row["id"])
        )
    if stale:
        conn.commit()

    return [_row_to_role(row) for row in stale]


def shortlist(conn: sqlite3.Connection):
    """Return HIGH-scored roles for the weekly digest shortlist."""
    rows = conn.execute(
        "SELECT * FROM roles WHERE score = ? ORDER BY last_seen_at DESC",
        (Score.HIGH.label,),
    ).fetchall()
    return [_row_to_role(row) for row in rows]
