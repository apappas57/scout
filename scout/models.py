from __future__ import annotations
import enum
import hashlib
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit


class Score(enum.Enum):
    LOW = ("LOW", 1)
    MED = ("MED", 2)
    HIGH = ("HIGH", 3)

    def __init__(self, label, rank):
        self.label = label
        self.rank = rank


_TRACKING_PREFIXES = ("utm_", "gh_src", "fbclid", "gclid")


def canonical_url(url: str) -> str:
    parts = urlsplit(url.strip())
    query = "&".join(
        kv for kv in parts.query.split("&")
        if kv and not any(kv.lower().startswith(p) for p in _TRACKING_PREFIXES)
    )
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), query, ""))


def role_id(role: "Role") -> str:
    key = f"{role.company.strip().lower()}|{role.title.strip().lower()}|{canonical_url(role.url)}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


@dataclass
class Role:
    company: str
    title: str
    url: str
    location: str
    source: str = ""
    snippet: str = ""
    score: Score | None = None
    score_reason: str = ""
    status: str = "new"            # new|shortlisted|applied|interview|rejected|offer|dropped
    drafts: dict = field(default_factory=dict)  # {"cover_letter": str, "resume_note": str, "warm_path": str}

    @property
    def id(self) -> str:
        return role_id(self)


@dataclass
class RunResult:
    run_id: str
    discovered: int = 0
    verified: int = 0
    new_qualified: int = 0
    high: int = 0
    drafted: int = 0
    errors: list[str] = field(default_factory=list)
