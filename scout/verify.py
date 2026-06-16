from __future__ import annotations

from scout.models import Role
from scout.filter_dedupe import _location_ok

# Markers that indicate a posting is closed, expired, or missing. Kept in one
# place so the closed-detection logic is easy to tune. All lowercase: matched
# against a lowercased page body.
CLOSED_MARKERS = (
    "no longer accepting",
    "position filled",
    "this position has been filled",
    "this role has been filled",
    "applications are closed",
    "job posting closed",
    "this job is no longer available",
    "404",
    "page not found",
    "not found",
)


def httpx_get(url: str) -> tuple[int, str]:
    """Default fetcher. Returns (status_code, body). Any network failure is
    swallowed into (0, "") so the role is treated as dead and dropped rather
    than crashing the run."""
    try:
        import httpx

        r = httpx.get(url, follow_redirects=True, timeout=20)
        return r.status_code, r.text[:5000]
    except Exception:
        return 0, ""


def _is_closed(text: str) -> bool:
    """True when the page body carries an obvious closed or expired marker."""
    hay = (text or "").lower()
    return any(marker in hay for marker in CLOSED_MARKERS)


def verify(roles: list[Role], *, fetch=httpx_get, filters: dict) -> list[Role]:
    """Keep only roles whose posting is live and still in an allowed location.

    A role survives when its URL returns HTTP 200, the page body carries no
    closed/expired/404 marker, and its location still passes the filters. This
    is the trust layer that kills dead-link and wrong-location waste before
    anything reaches the draft stage. ``fetch`` is injected so tests run fully
    offline; it returns ``(status_code, text)``."""
    out: list[Role] = []
    for role in roles:
        status, text = fetch(role.url)
        if status != 200:
            continue
        if _is_closed(text):
            continue
        if not _location_ok(role.location, filters):
            continue
        out.append(role)
    return out
