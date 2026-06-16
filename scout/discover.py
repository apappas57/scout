"""Discover stage: ask the LLM (with web access) for live applied-AI / FDE role
candidates and parse them into Role objects.

This is an LLM-bound stage. The client is injected so tests run against recorded
fixtures via FakeLLMClient and the live path shells out to `claude -p`.
"""
from __future__ import annotations
import json
import re

from scout.models import Role
from scout.llm import LLMError


class DiscoverError(RuntimeError):
    """Raised when the LLM response cannot be parsed into role candidates.

    The runner catches this, logs it to the audit table, and continues so a
    bad discovery call never aborts the whole weekly run.
    """


# Fields lifted from each candidate object into a Role. Anything else the LLM
# returns is ignored.
_ROLE_FIELDS = ("company", "title", "url", "location", "source", "snippet")


def build_prompt(search: dict) -> str:
    """Compose the discovery prompt from the search config.

    No em dashes anywhere in the generated text (commas, colons, full stops
    only), per the project content rules.
    """
    queries = search.get("queries") or []
    companies = search.get("target_companies") or []
    boards = search.get("boards") or []

    lines = [
        "You are a job-hunt research assistant. Find currently live, applied-AI "
        "and Forward Deployed Engineer roles that are open right now.",
        "",
        "Use web search to confirm each posting exists and is still accepting "
        "applications. Do not invent roles or URLs.",
        "",
    ]
    if queries:
        lines.append("Search terms to cover: " + ", ".join(queries) + ".")
    if companies:
        lines.append("Prioritise these target companies: " + ", ".join(companies) + ".")
    if boards:
        lines.append("Likely job boards to check: " + ", ".join(boards) + ".")
    lines += [
        "",
        "Return ONLY a strict JSON array. Each element is an object with these "
        "keys: company, title, url, location, source, snippet.",
        "The url must be the direct, live application link. The snippet is a "
        "one-line summary of the role. Output no prose before or after the array.",
    ]
    return "\n".join(lines)


def _extract_json_array(text: str) -> list:
    """Tolerantly pull the first JSON array out of the LLM response.

    Handles prose wrapped around the array. Raises DiscoverError if no array is
    present or it is not valid JSON.
    """
    match = re.search(r"\[.*\]", text, re.S)
    if not match:
        raise DiscoverError("discover: no JSON array found in LLM response")
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise DiscoverError(f"discover: JSON array did not parse: {exc}") from exc
    if not isinstance(data, list):
        raise DiscoverError("discover: parsed JSON was not a list")
    return data


def _to_role(item: dict) -> Role:
    return Role(
        company=str(item.get("company", "")).strip(),
        title=str(item.get("title", "")).strip(),
        url=str(item.get("url", "")).strip(),
        location=str(item.get("location", "")).strip(),
        source=str(item.get("source", "")).strip(),
        snippet=str(item.get("snippet", "")).strip(),
    )


def _dedupe(roles: list[Role]) -> list[Role]:
    """Drop duplicate roles (same id) within a batch, keeping the first seen."""
    seen, out = set(), []
    for r in roles:
        if r.id not in seen:
            seen.add(r.id)
            out.append(r)
    return out


def _discover_one(client, search: dict, timeout: int) -> list[Role]:
    raw = client.complete(build_prompt(search), web=True, timeout=timeout)
    return [_to_role(i) for i in _extract_json_array(raw) if isinstance(i, dict)]


def discover(client, *, search: dict) -> list[Role]:
    """Run the discovery stage.

    When target_companies are set, search each company in its own web call
    (per-company batching). Each call stays focused and fast, and the loop is
    partial-safe: if one company's call times out or returns junk, that company
    is skipped and the rest still return. With no target_companies, runs a single
    general search (and DiscoverError from it propagates for the runner to catch).
    Each web call is bounded by search.discover_timeout (default 240s per company).
    """
    companies = search.get("target_companies") or []
    timeout = int(search.get("discover_timeout", 240))
    if not companies:
        return _dedupe(_discover_one(client, search, timeout))
    roles: list[Role] = []
    for company in companies:
        sub = dict(search, target_companies=[company])
        try:
            roles.extend(_discover_one(client, sub, timeout))
        except (DiscoverError, LLMError):
            continue  # partial-safe: skip this company, keep the others
    return _dedupe(roles)
