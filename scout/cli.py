"""Scout command-line interface.

Subcommands:
    run [--dry-run] [--demo] [--config PATH]
        Run the weekly loop once. The live path wires the real ClaudeCliClient,
        a real httpx fetch, and real digest delivery. --dry-run discovers, scores,
        and previews but writes nothing. --demo runs fully offline against bundled
        fixtures: zero LLM calls, zero network requests.
    init
        Write a starter private config (config/scout.config.toml) from the example.
        Never overwrites an existing private config.
    status
        Print the last run and the heartbeat.

Safety: a live run refuses to start when config["enabled"] is false, unless it is
a --demo or --dry-run invocation. Scout never submits or sends anything: the only
autonomous output of a live run is the weekly digest, delivered only to Alex.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from scout import config as config_mod
from scout import db as db_mod
from scout import runner
from scout.digest import _default_notify, _default_write
from scout.llm import ClaudeCliClient, FakeLLMClient
from scout.verify import httpx_get

_REPO_ROOT = Path(__file__).resolve().parent.parent
_EXAMPLE_CONFIG = _REPO_ROOT / "config" / "scout.config.example.toml"
_PRIVATE_CONFIG = _REPO_ROOT / "config" / "scout.config.toml"
_FIXTURE = _REPO_ROOT / "fixtures" / "discover_sample.json"


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


# --- demo wiring (fully offline) -------------------------------------------------

# Canned draft and critique responses so --demo can run the draft stage without any
# live LLM call. No em dashes anywhere, per the content rules.
_DEMO_DRAFT = json.dumps({
    "cover_letter": "Dear hiring team, I am applying because this role matches my "
                    "applied-AI delivery work. I have shipped customer-facing AI "
                    "integrations end to end and would bring that directly to you.",
    "resume_note": "Lead with the applied-AI delivery projects and the measurable "
                   "outcomes, then the data-engineering depth.",
    "warm_path": "",
})
_DEMO_CRITIQUE = json.dumps({"ok": True, "issues": []})


def _demo_client() -> FakeLLMClient:
    """A FakeLLMClient that answers every stage from fixtures and canned text.

    The dict matches by the first key found as a substring of the prompt, so the
    keys are chosen to map cleanly to each stage's prompt. The discover prompt is
    served the bundled fixture; draft and critique get canned JSON.
    """
    fixture_text = _FIXTURE.read_text(encoding="utf-8")
    return FakeLLMClient({
        # discover.build_prompt
        "strict JSON array": fixture_text,
        # draft._build_critique_prompt (checked before the draft key, no overlap)
        "Critique this job application draft": _DEMO_CRITIQUE,
        # draft._build_revision_prompt
        "Revise this job application draft": _DEMO_DRAFT,
        # draft._build_draft_prompt
        "cover letter": _DEMO_DRAFT,
    })


def _demo_fetch(url: str):
    """Stub fetcher for demo mode. Every URL is live and remote. No network."""
    return (200, "Apply now. Remote. Great applied AI role.")


# --- subcommands -----------------------------------------------------------------

def _load(config_path: str | None) -> dict:
    if config_path:
        return config_mod.load_config(public=_EXAMPLE_CONFIG, private=Path(config_path))
    return config_mod.load_config(public=_EXAMPLE_CONFIG, private=_PRIVATE_CONFIG)


def cmd_run(args) -> int:
    cfg = _load(args.config)

    if not cfg.get("enabled", False) and not (args.demo or args.dry_run):
        print(
            "Scout is disabled (config enabled = false). "
            "Set enabled = true in config/scout.config.toml, "
            "or run with --demo or --dry-run.",
            file=sys.stderr,
        )
        return 1

    if args.demo:
        client = _demo_client()
        fetch = _demo_fetch
        write = _default_write
        notify = _default_notify
    else:
        client = ClaudeCliClient()
        fetch = httpx_get
        write = _default_write
        notify = _default_notify

    db_path = cfg.get("paths", {}).get("db", "scout.db")
    # In demo or dry-run, keep the real db untouched: use an in-memory database so
    # nothing private is created or written on a smoke run.
    if args.demo or args.dry_run:
        conn = db_mod.connect(":memory:")
    else:
        conn = db_mod.connect(db_path)

    try:
        result = runner.run(
            config=cfg,
            conn=conn,
            client=client,
            now=_now_iso(),
            fetch=fetch,
            write=write,
            notify=notify,
            dry_run=args.dry_run,
        )
    finally:
        conn.close()

    mode = "dry-run" if args.dry_run else "live"
    if args.demo:
        mode = f"{mode} demo"
    print(f"Scout run complete ({mode}).")
    print(
        f"  discovered={result.discovered} verified={result.verified} "
        f"new_qualified={result.new_qualified} high={result.high} "
        f"drafted={result.drafted} errors={len(result.errors)}"
    )
    for err in result.errors:
        print(f"  ! {err}")

    preview = getattr(result, "digest", "")
    if preview:
        print()
        print("----- digest preview -----")
        print(preview)
        print("--------------------------")
    return 0


def cmd_init(args) -> int:
    if _PRIVATE_CONFIG.exists():
        print(f"Private config already exists, not overwriting: {_PRIVATE_CONFIG}")
        return 0
    _PRIVATE_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(_EXAMPLE_CONFIG, _PRIVATE_CONFIG)
    print(f"Wrote starter private config: {_PRIVATE_CONFIG}")
    print("Edit it with your profile, paths, and ntfy topic. It is gitignored.")
    return 0


def cmd_status(args) -> int:
    cfg = _load(args.config)
    db_path = cfg.get("paths", {}).get("db", "scout.db")

    if not Path(db_path).exists():
        print(f"No scout.db yet at {db_path}. Run `scout run` first.")
    else:
        conn = db_mod.connect(db_path)
        try:
            row = conn.execute(
                "SELECT run_id, started_at, finished_at, status, counts_json, error "
                "FROM runs ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            print("No runs recorded yet.")
        else:
            print("Last run")
            print(f"  run_id:    {row['run_id']}")
            print(f"  started:   {row['started_at']}")
            print(f"  finished:  {row['finished_at']}")
            print(f"  status:    {row['status']}")
            if row["counts_json"]:
                counts = json.loads(row["counts_json"])
                pretty = " ".join(f"{k}={v}" for k, v in counts.items())
                print(f"  counts:    {pretty}")
            if row["error"]:
                print(f"  error:     {row['error']}")

    heartbeat = cfg.get("paths", {}).get("heartbeat")
    if heartbeat and Path(heartbeat).exists():
        print(f"Heartbeat: {Path(heartbeat).read_text().strip()}")
    else:
        print("Heartbeat: none")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scout",
        description="Scout: autonomous weekly applied-AI job-hunt agent. Assistive by design.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="run the weekly loop once")
    p_run.add_argument("--dry-run", action="store_true",
                       help="discover, verify, filter, and score but write and send nothing")
    p_run.add_argument("--demo", action="store_true",
                       help="run fully offline against bundled fixtures (no LLM, no network)")
    p_run.add_argument("--config", default=None,
                       help="path to a private config TOML (overrides the default)")
    p_run.set_defaults(func=cmd_run)

    p_init = sub.add_parser("init", help="write a starter private config (never overwrites)")
    p_init.set_defaults(func=cmd_init)

    p_status = sub.add_parser("status", help="print the last run and the heartbeat")
    p_status.add_argument("--config", default=None,
                          help="path to a private config TOML (overrides the default)")
    p_status.set_defaults(func=cmd_status)

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
