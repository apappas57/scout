"""Draft stage: produce a cover letter and resume-emphasis note for a HIGH role,
then run a self-critique gate with one revision pass. LLM-bound via LLMClient.

Safety: this stage only WRITES drafts into role.drafts. It never sends anything.
Content rule: prompts forbid em dashes, generic filler, and overclaiming skills
not present in the CV.
"""
from __future__ import annotations
import json
import re

from scout.llm import LLMClient, LLMError
from scout.models import Role, Score

# One shared statement of the content rules so the draft and revision prompts
# stay consistent. No em dashes anywhere: commas, colons, and full stops only.
_CONTENT_RULES = (
    "Content rules: do not use em dashes anywhere, use commas, colons or full "
    "stops instead. No generic filler or boilerplate openers. Do not overclaim "
    "skills or experience that are not in the CV. Plain text, Australian English."
)


def _build_draft_prompt(role: Role, profile: dict, cv_text: str) -> str:
    return (
        "You are helping Alex apply for a role he has shortlisted. Write a tailored "
        "cover letter and a short resume-emphasis note.\n\n"
        f"Role: {role.title} at {role.company} ({role.location}).\n"
        f"Posting snippet: {role.snippet}\n"
        f"Applicant profile: {json.dumps(profile)}\n"
        f"CV text (ground every claim in this, do not invent): {cv_text}\n\n"
        f"{_CONTENT_RULES}\n\n"
        "Return ONLY a JSON object with exactly these keys: "
        '{"cover_letter": str, "resume_note": str, "warm_path": str}. '
        "warm_path is a known contact or warm intro path, or an empty string if none."
    )


def _build_critique_prompt(role: Role, draft: dict) -> str:
    return (
        "Critique this job application draft strictly. Flag generic filler, "
        "overclaiming beyond the CV, weak or templated openers, and any em dashes.\n\n"
        f"Role: {role.title} at {role.company}.\n"
        f"Draft: {json.dumps(draft)}\n\n"
        'Return ONLY a JSON object: {"ok": bool, "issues": [str, ...]}. '
        "ok is true only if the draft is specific, honest, and free of the problems above."
    )


def _build_revision_prompt(role: Role, profile: dict, cv_text: str, draft: dict, issues: list) -> str:
    return (
        "Revise this job application draft to fix every issue listed. Keep it "
        "specific and grounded in the CV.\n\n"
        f"Role: {role.title} at {role.company} ({role.location}).\n"
        f"Applicant profile: {json.dumps(profile)}\n"
        f"CV text (ground every claim in this, do not invent): {cv_text}\n"
        f"Previous draft: {json.dumps(draft)}\n"
        f"Issues to fix: {json.dumps(issues)}\n\n"
        f"{_CONTENT_RULES}\n\n"
        "Return ONLY a JSON object with exactly these keys: "
        '{"cover_letter": str, "resume_note": str, "warm_path": str}.'
    )


def _parse_json_object(text: str) -> dict:
    """Tolerantly extract the first JSON object from an LLM response."""
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        raise LLMError(f"draft stage: no JSON object found in LLM output: {text!r}")
    try:
        obj = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise LLMError(f"draft stage: invalid JSON in LLM output: {exc}") from exc
    if not isinstance(obj, dict):
        raise LLMError("draft stage: expected a JSON object")
    return obj


def draft_role(client: LLMClient, role: Role, *, profile: dict, cv_text: str = "") -> Role:
    """Populate role.drafts with a cover letter, resume note, and warm path for a
    HIGH role, gated by a self-critique pass with one revision. If the role is not
    HIGH, returns it unchanged. If the draft still fails critique after the single
    revision, stores the issues in drafts['_flag'] so a human can review."""
    if role.score is not Score.HIGH:
        return role

    draft = _parse_json_object(client.complete(_build_draft_prompt(role, profile, cv_text)))
    critique = _parse_json_object(client.complete(_build_critique_prompt(role, draft)))

    if not critique.get("ok", False):
        issues = critique.get("issues", [])
        draft = _parse_json_object(
            client.complete(_build_revision_prompt(role, profile, cv_text, draft, issues))
        )
        critique = _parse_json_object(client.complete(_build_critique_prompt(role, draft)))

    role.drafts = {
        "cover_letter": draft.get("cover_letter", ""),
        "resume_note": draft.get("resume_note", ""),
        "warm_path": draft.get("warm_path", ""),
    }
    if not critique.get("ok", False):
        role.drafts["_flag"] = "; ".join(critique.get("issues", [])) or "critique failed"
    return role
