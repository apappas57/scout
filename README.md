# Scout

Scout is an autonomous weekly agent that runs an applied-AI, Forward-Deployed-Engineer
and AI-native-engineering job hunt end to end. It discovers live roles from company ATS
boards and from LLM web search, verifies they are real and open, filters them against
hard constraints, gates them on genuine remote eligibility, scores them, ranks them by
how *uncontested* they probably are, drafts tailored application material for the best
matches, tracks the pipeline, and hands back a ready-to-review shortlist.

The point of the ATS source is to find roles before they reach the job boards everyone
else is refreshing. See [The ATS path](#the-ats-path).

Scout is assistive by design. It finds and drafts. A human reviews and submits. Scout
never submits an application or sends a message on anyone's behalf. The only autonomous
output is a weekly digest, delivered only to its owner.

## The weekly loop

```
  ats  ----\
  (json)    >-- merge -> verify -> filter + dedupe -> remote gate -> score -> contest -> draft -> track -> digest
  llm  ----/    (dedupe)  (httpx)   (pure logic)      (signals)      (rules)  (proxy)    (LLM)   (sqlite) (file+ntfy)
  (LLM+web)
```

1. **discover** runs two independent sources into the same pipeline.
   - **ats** reads company-owned applicant tracking boards directly over their public
     JSON endpoints. No LLM, no API key, fully deterministic. See below.
   - **llm** asks an LLM (with web search) for currently live roles and parses them
     into structured candidates.
   Both batches are merged and deduplicated by role id, so a role found twice is
   drafted once, and either source can fail without taking the other down.
2. **verify** fetches each posting over HTTP and keeps only the ones that return 200,
   carry no "closed" or "no longer accepting" marker, and still match the location rules.
   This is the trust layer that kills dead links and wrong-location noise before anything
   reaches the draft stage. ATS roles skip this by default: the board API only returns
   open postings, so re-fetching every apply URL is a slow second opinion on a fact
   already established, and several boards render the posting in JavaScript so the
   closed-marker scan is unreliable there. Set `ats.verify_http = true` to force it.
3. **filter + dedupe** drops already-seen roles, in-batch duplicates, excluded locations,
   and excluded keywords. Pure, deterministic, unit-tested.
4. **remote gate** re-reads each surviving posting's text for residency,
   work-authorisation, onsite and timezone clauses. Substring location matching passes a
   posting whose location field says "Remote" and whose body says US-only; this catches
   it. Conservative by design: it blocks only on high-confidence evidence, and every
   block is written to the audit table with the sentence that caused it.
5. **score** assigns HIGH / MED / LOW with a one-line human reason, using rules over
   target companies, role-type keywords, seniority, and profile signals. No LLM in the
   scoring path, so it is fast and testable.
6. **contest proxy** attaches a transparent 0-100 estimate of how *under-contested* each
   shortlisted role probably is, from posting freshness, whether the role has escaped to
   aggregators, company obscurity, title specificity and location narrowness. Higher
   means quieter. It is a proxy and never an applicant count, because no board publishes
   one, and every surface says so.
7. **draft** writes a tailored cover letter and resume-emphasis note for each HIGH role,
   then runs a self-critique gate with one revision pass. Prompts forbid generic filler
   and overclaiming anything not in the CV.
8. **track** upserts roles into SQLite idempotently and detects stale applications that
   are due a follow-up.
9. **digest** composes a plain-text brief and delivers it to a dated file plus an ntfy
   push. Nothing else leaves the machine. The brief opens with **Least contested first**,
   ordered by the contest proxy rather than by fit score, because the weekly question is
   not "what scores highest", it is "what should I apply to that others have not found".

## The ATS path

### Why it exists

The LLM discovery stage asks a model with web access to find roles. That can only
surface postings a search engine has already indexed, which is the same pool Seek and
LinkedIn aggregate. By the time a role is indexed, it is high-volume: hundreds of
applications are already in.

A company's own ATS board is where a posting appears *first*. Reading those boards
directly is how Scout gets there early. It is also deterministic, free, needs no API key
and no LLM, and returns the same answer twice, none of which is true of the LLM path.

The two are complements, not replacements. The LLM finds companies not in the registry;
the ATS path finds roles before anyone else does.

### Supported boards

All verified live against real companies, no authentication:

| Board | Endpoint |
| --- | --- |
| Greenhouse | `https://boards-api.greenhouse.io/v1/boards/{slug}/jobs` |
| Lever | `https://api.lever.co/v0/postings/{slug}?mode=json` |
| Ashby | `https://api.ashbyhq.com/posting-api/job-board/{slug}` |
| SmartRecruiters | `https://api.smartrecruiters.com/v1/companies/{slug}/postings` |

Workable is deliberately absent. Its public widget endpoint either 404s or answers 200
with an empty jobs array, and the v3 endpoint returns a bot challenge. A board that
silently returns nothing is worse than no board at all, because it reads as "this company
is not hiring".

Every network call is non-fatal. One dead board, one renamed slug, or one company that
migrated ATS is logged and skipped. It can never cost you the rest of the week's roles.

### Adding a company to the ATS registry

The registry comes from two places, merged and deduplicated on `(board, slug)`:

1. **`config/ats_companies.toml`**, pointed at by `[ats] registry_file`. This is the main
   list, one annotated `[[company]]` block per board with a `verified` date and a note on
   why the company is worth polling. Rows here use `name` for the display name.
2. **`[ats] companies`** in your private config, for additions not yet in the shared
   file. Rows here use `company`.

Either spelling works; the runner normalises them. A row missing a name, board or slug is
skipped with a warning rather than silently polling the wrong thing.

```toml
# config/ats_companies.toml
[[company]]
name = "Anthropic"
board = "greenhouse"
slug = "anthropic"
verified = "2026-08-02"

# or, inline in scout.config.toml
companies = [
  { company = "Anthropic", board = "greenhouse", slug = "anthropic" },
]
```

The display name is what lands in `Role.company`, so write it the way you would say it.
`slug` is that company's id on that *specific* ATS, and it is frequently not the company
name lowercased. Canva is on SmartRecruiters as `Canva`; Glean is on Greenhouse as
`gleanwork`; Heidi Health is on Ashby as `heidihealth.com.au`.

**Verify the slug before you commit it.** A registry full of 404s is worse than a short
one:

```bash
.venv/bin/python -c "
from scout.ats import verify_slug
print(verify_slug('greenhouse', 'anthropic'))
"
# (True, 400, 'ok')
```

`verify_slug` returns `(ok, posting_count, note)` and only says ok on HTTP 200 in the
right shape **with a non-zero posting count**, because a board answering 200 with an
empty list is indistinguishable from a wrong slug. If you get `200 but zero postings`,
that company is on a different ATS: try another board before giving up.

Both registry files list the companies that were probed and rejected, so nobody re-tries
a slug that has already been ruled out.

A full pass over the shipped 155-company registry returns roughly 770 postings and takes
about 30 seconds without descriptions, or about 3 minutes with descriptions and the
aggregator probe on.

### Running ATS-only

The ATS path needs no LLM at all, so it is the cheap, deterministic pass worth putting on
a schedule. Select sources with the `SCOUT_SOURCES` environment variable:

```bash
# deterministic, no LLM calls at all
SCOUT_SOURCES=ats .venv/bin/python -m scout.cli run

# preview it, writing and sending nothing
SCOUT_SOURCES=ats .venv/bin/python -m scout.cli run --dry-run

# the LLM path on its own
SCOUT_SOURCES=llm .venv/bin/python -m scout.cli run
```

The same choice can be made permanent in config:

```toml
[sources]
ats = true
llm = false
```

Precedence is the `sources` argument to `runner.run`, then `SCOUT_SOURCES`, then
`[sources]`, then both. An unrecognised value falls back to both rather than silently
discovering nothing.

A full ATS-only pass makes roughly one request per company, plus description fetches
capped by `ats.max_detail`, all spaced by `ats.delay`. On the shipped 155-company
registry that is about 30 seconds with `with_content = false`, or about 3 minutes with
descriptions and the aggregator probe on.

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
unit-tested. The LLM and web-bound stages (`discover`, `ats`, `signals`, `verify`,
`draft`) sit behind injectable interfaces: the LLM client, the HTTP fetcher, the ATS
JSON fetcher, the aggregator probe and the write/notify side effects are all passed in,
so the whole loop runs offline in tests against recorded fixtures, and the live path
shells out to `claude -p` for headless LLM calls. State and the application tracker live
in SQLite. A weekly launchd job runs the loop unattended behind a heartbeat wrapper, and
a `/hunt` skill runs it on demand.

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
# then edit config/scout.config.toml: profile, CV path, search terms, ntfy topic,
# and the [ats] companies registry (verify every slug first, see above)

# 3. Try it without touching anything (offline, bundled fixtures)
SCOUT_SOURCES=llm .venv/bin/python -m scout.cli run --dry-run --demo

# 4. The cheap deterministic pass: real boards, no LLM, writing nothing
SCOUT_SOURCES=ats .venv/bin/python -m scout.cli run --dry-run

# 5. A real dry run across both sources, writing nothing
.venv/bin/python -m scout.cli run --dry-run

# 6. A full run
.venv/bin/python -m scout.cli run

# Check the last run and the heartbeat
.venv/bin/python -m scout.cli status
```

### Demo mode

`run --demo` seeds the LLM from a bundled, anonymised fixture and stubs every liveness
check as live, so the LLM path costs nothing and touches no network. Pair it with
`--dry-run` for a read-only preview. This is the mode to use on an interview
screen-share: it shows the whole loop end to end without touching the real database or
revealing any private data.

One caveat: `--demo` does not yet stub the ATS boards, so with the ATS source enabled it
will make real requests to the configured company boards. Run it as
`SCOUT_SOURCES=llm ... --demo` for the fully offline demo. `runner.run` already accepts
an `ats_get_json` fetcher for this; wiring it to `--demo` in `scout/cli.py` restores the
zero-network guarantee for both sources.

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
