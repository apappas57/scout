from __future__ import annotations
from scout.models import Role


def _location_ok(location: str, filters: dict) -> bool:
    """A location passes when it contains no excluded substring and, when
    remote_only is set, contains at least one allowed substring."""
    hay = (location or "").lower()
    for bad in filters.get("exclude_locations", []):
        if bad.lower() in hay:
            return False
    if filters.get("remote_only"):
        allowed = filters.get("allow_locations", [])
        if not any(good.lower() in hay for good in allowed):
            return False
    return True


def _keywords_ok(role: Role, filters: dict) -> bool:
    """Reject when any excluded keyword appears in the title or snippet."""
    hay = f"{role.title} {role.snippet}".lower()
    return not any(kw.lower() in hay for kw in filters.get("exclude_keywords", []))


def filter_roles(roles: list[Role], *, seen_ids: set[str], filters: dict) -> list[Role]:
    """Drop roles that are already seen, duplicated within the batch, in an
    excluded/non-allowed location, or carry an excluded keyword. Keeps the
    first occurrence of any duplicate id."""
    out: list[Role] = []
    batch_seen: set[str] = set(seen_ids)
    for role in roles:
        rid = role.id
        if rid in batch_seen:
            continue
        if not _location_ok(role.location, filters):
            batch_seen.add(rid)
            continue
        if not _keywords_ok(role, filters):
            batch_seen.add(rid)
            continue
        batch_seen.add(rid)
        out.append(role)
    return out
