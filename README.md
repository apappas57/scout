# Scout

Scout is an autonomous weekly agent that runs an applied-AI / Forward-Deployed-Engineer
job hunt end to end. It discovers live roles, verifies they are real and open, filters
them against hard constraints, scores them, drafts tailored application material for the
best matches, tracks the pipeline, and hands back a ready-to-review shortlist.

Scout is assistive by design. It finds and drafts. A human reviews and submits. Scout
never submits an application or sends a message on anyone's behalf. The only autonomous
output is a weekly digest, delivered only to its owner.

## The weekly loop

```
  discover  ->  verify  ->  filter + dedupe  ->  score  ->  draft  ->  track  ->  digest
  (LLM+web)     (httpx)      (pure logic)        (rules)    (LLM)       (sqlite)   (file+ntfy)
```

1. **discover** asks an LLM (with web search) for currently live applied-AI and FDE
   roles and parses them into structured candidates.
2. **verify** fetches each posting over HTTP and keeps only the ones that return 200,
   carry no "closed" or "no longer accepting" marker, and still match the location rules.
   This is the trust layer that kills dead links and wrong-location noise before anything
   reaches the draft stage.
3. **filter + dedupe** drops already-seen roles, in-batch duplicates, excluded locations,
   and excluded keywords. Pure, deterministic, unit-tested.
4. **score** assigns HIGH / MED / LOW with a one-line human reason, using rules over
   target companies, role-type keywords, seniority, and profile signals. No LLM in the
   scoring path, so it is fast and testable.
5. **draft** writes a tailored cover letter and resume-emphasis note for each HIGH role,
   then runs a self-critique gate with one revision pass. Prompts forbid generic filler
   and overclaiming anything not in the CV.
6. **track** upserts roles into SQLite idempotently and detects stale applications that
   are due a follow-up.
7. **digest** composes a plain-text brief and delivers it to a dated file plus an ntfy
   push. Nothing else leaves the machine.

## Safety and privacy model

- **Never autonomous outward.** Scout writes drafts and a digest. It does not send,
  apply, or message. The weekly digest is delivered only to its owner.
- **One kill switch.** A single `enabled` flag in config disables the whole agent. A
  live run refuses to start when `enabled` is false.
- **Dry run.** `run --dry-run` discovers, verifies, filters, and scores, then previews
  the digest. It writes nothing to the database, drafts nothing, and sends nothing.
- **All PII stays local and out of git.** The CV path, profile, target compensation,
  real roles, and the entire SQLite database live in a gitignored private config and a
  gitignored `scout.db`. The code in this repo is safe to publish. The privacy gate in
  the build verifies that no private file is ever tracked.

## How it is built

Scout is a small Python package of single-responsibility stages wired by a runner.
Pure-logic stages (`filter_dedupe`, `score`, `state`, `track`) are deterministic and
unit-tested. The LLM and web-bound stages (`discover`, `verify`, `draft`) sit behind a
single injectable interface, so the whole loop runs fully offline in tests against
recorded fixtures, and the live path shells out to `claude -p` for headless LLM calls.
State and the application tracker live in SQLite. A weekly launchd job runs the loop
unattended behind a heartbeat wrapper, and a `/hunt` skill runs it on demand.

Every stage in the runner is wrapped in its own guard: a single stage failure is logged
to an audit table, recorded on the run result, and the run continues with partial
results rather than aborting the week.

Stack: Python 3.11+ (stdlib `tomllib`, `sqlite3`, `argparse`, `subprocess`,
`dataclasses`), `httpx` for liveness checks, `pytest` for tests, `claude -p` for headless
LLM calls, launchd plus ntfy for scheduling and alerts.

## Quick start

```bash
# 1. Set up the environment
cd ~/dev/career/scout
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2. Create your private config (gitignored, never overwrites an existing one)
.venv/bin/python -m scout.cli init
# then edit config/scout.config.toml: profile, CV path, search terms, ntfy topic

# 3. Try it without touching anything (offline, bundled fixtures)
.venv/bin/python -m scout.cli run --dry-run --demo

# 4. A real dry run against live discovery, writing nothing
.venv/bin/python -m scout.cli run --dry-run

# 5. A full run
.venv/bin/python -m scout.cli run

# Check the last run and the heartbeat
.venv/bin/python -m scout.cli status
```

### Demo mode

`run --demo` runs the entire pipeline fully offline: zero live LLM calls and zero
network requests. It seeds the LLM from a bundled, anonymised fixture and stubs every
liveness check as live. Pair it with `--dry-run` for a read-only preview. This is the
mode to use on an interview screen-share: it shows the whole loop end to end without
touching the real database or revealing any private data.

## Schedule

A launchd job runs Scout every Sunday at 20:00 AEST. It is not auto-loaded by the
build. When you are ready to schedule it:

```bash
launchctl bootstrap gui/$(id -u) ~/dev/career/scout/launchd/com.alex.scout-weekly.plist
```

To stop it:

```bash
launchctl bootout gui/$(id -u)/com.alex.scout-weekly
```

The job runs through `scripts/run_scout.sh`, which writes a heartbeat and fires a
durable ntfy alert on any failure. No secrets live in the script: the ntfy topic is read
at runtime from the gitignored private config.

## Tests

```bash
.venv/bin/pytest
```

Every stage has a focused unit test, and the runner has an end-to-end dry-run test that
exercises the whole loop offline.
