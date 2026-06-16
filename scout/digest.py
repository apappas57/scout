from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path

from scout.models import Role, RunResult


def _high_block(role: Role) -> str:
    marker = "draft ready" if role.drafts and "_flag" not in role.drafts else "draft pending"
    reason = role.score_reason or "no reason recorded"
    return (
        f"- {role.company}: {role.title} [{marker}]\n"
        f"  {role.location} | {reason}\n"
        f"  {role.url}"
    )


def _med_line(role: Role) -> str:
    reason = role.score_reason or "no reason recorded"
    return f"- {role.company}: {role.title} ({role.location}) | {reason}"


def _stale_line(role: Role) -> str:
    return f"- {role.company}: {role.title} | follow-up due, no reply"


def compose_digest(*, shortlist: list[Role], med: list[Role], stale: list[Role], run: RunResult) -> str:
    """Build a plain-text weekly brief. No em dashes anywhere."""
    lines: list[str] = []
    lines.append("Scout weekly digest")
    lines.append("===================")
    lines.append("")
    lines.append(
        f"Discovered {run.discovered}, verified {run.verified}, "
        f"new qualified {run.new_qualified}, HIGH {run.high}, drafted {run.drafted}."
    )
    if run.errors:
        lines.append(f"Stage errors: {len(run.errors)} (see audit log).")
    lines.append("")

    lines.append(f"HIGH priority ({len(shortlist)})")
    lines.append("-------------")
    if shortlist:
        for role in shortlist:
            lines.append(_high_block(role))
    else:
        lines.append("None this run.")
    lines.append("")

    lines.append(f"Worth a look ({len(med)})")
    lines.append("-------------")
    if med:
        for role in med:
            lines.append(_med_line(role))
    else:
        lines.append("None this run.")
    lines.append("")

    lines.append(f"Follow-ups due ({len(stale)})")
    lines.append("--------------")
    if stale:
        for role in stale:
            lines.append(_stale_line(role))
    else:
        lines.append("None due.")
    lines.append("")

    lines.append("Scout finds and drafts. You review and submit.")

    return "\n".join(lines)


def _default_write(path, text: str) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _default_notify(text: str) -> None:  # pragma: no cover - live path
    pass


def deliver(
    text: str,
    *,
    digests_dir: str,
    ntfy_topic: str = "",
    now: str | None = None,
    write=_default_write,
    notify=_default_notify,
) -> str:
    """Write the digest to a dated file and fire an ntfy notice. Both side effects are injected for tests."""
    if now is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    else:
        stamp = now.replace("-", "")[:8]
    path = str(Path(digests_dir) / f"scout-{stamp}.md")
    write(path, text)
    if ntfy_topic:
        first_line = text.splitlines()[0] if text else "Scout digest ready"
        notify(first_line)
    return path
