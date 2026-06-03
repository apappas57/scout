# Scout: Autonomous Job-Hunt Agent (Design)

**Status:** Designed 2 June 2026. Not yet built. v1 target: one weekend.
**Owner:** Alex Pappas.
**One-liner:** An autonomous weekly agent that runs Alex's applied-AI job hunt end to end (discover, verify, score, draft, track) and hands him a ready-to-fire shortlist. Assistive by design: Scout finds and drafts, Alex reviews and submits. Nothing is applied or sent on his behalf.

## Why
The bottleneck in the job hunt is volume of quality applications and warm outreach, not capability or CV. Scout removes that bottleneck by doing the finding and drafting on autopilot, so Alex spends his time only on review and the human moments (interviews, conversations). Second-order value: the agent itself is the strongest portfolio artifact for an applied-AI or Forward-Deployed Engineer role. "I built an agent that runs my own job hunt, here it is running" is the demo.

## The weekly loop
One scheduled run executes these stages in order:
1. Discover: search the live market (target-company boards + AU AI-native + job boards) for applied-AI, FDE, and solutions roles.
2. Verify: confirm each candidate role is currently open and location-correct before it goes further. Kills dead, closed, or wrong-location listings.
3. Filter + dedupe: drop anything breaking Alex's rules (remote-only, no Sydney or US relocation, comfort-led), and anything already in the tracker or seen in a prior run.
4. Score: rank survivors HIGH, MED, or LOW against Alex's profile and calibration, each with a one-line rationale.
5. Draft (HIGH only): generate a tailored cover letter + resume-emphasis note from the current CV, run a self-critique pass, and find a warm-intro path into the company where one exists.
6. Track + digest: log everything to the Applied-AI tracker, flag stale applications with follow-up drafts, surface warm-node reminders, and email Alex one weekly brief with the ranked shortlist and the drafts ready to fire.

## Architecture
Small, isolated units. Each does one job, is testable alone, and communicates through plain data (lists of role records):
- `config`: Alex's profile, calibration, target companies, search queries, filters, paths. One editable file. Private (see Privacy layer).
- `discover`: runs the searches, returns raw role candidates (company, title, url, location, snippet).
- `verify`: checks each candidate is live and location-correct. Drops the rest.
- `filter_dedupe`: applies constraints, dedupes against the seen-store and the tracker. Returns new qualifying roles.
- `score`: fit-scores each role, returns it annotated HIGH, MED, or LOW with a why.
- `draft`: for HIGH roles, produces cover letter + resume note + warm-path, each draft passing a self-critique gate.
- `track`: writes new roles and statuses to the tracker, detects stale apps, computes warm-node reminders.
- `digest`: composes the weekly brief and delivers it to Alex (Gmail draft to self, plus ntfy).
- `state`: the seen-roles store (idempotency), the per-run audit log, the run heartbeat.
- `runner`: orchestrates the stages. The entry point for both the schedule and a manual run.

## Data flow
config + state to discover to verify to filter_dedupe to score to draft (HIGH only) to track to digest to notify. State updates each run (seen roles appended, heartbeat written, audit log appended). A failure in any stage is caught, logged, and heartbeat-alerted; the run continues with partial results where sensible.

## Where it lives and how it runs
- Repo: `~/dev/scout`. A standalone repo, not buried in the agent OS, so it is a clean, shareable artifact. It reads the CV, resume generators, and tracker via configured paths.
- Autonomous: a weekly launchd job (local, because it needs the CV and tracker, which the cloud sandbox lacks), wrapped with the `run_job.sh` heartbeat and failure-alert pattern. Suggested cadence: Sunday about 20:00 AEST, so the digest is waiting Monday morning.
- Manual: a thin `/hunt` skill in `~/.claude/skills` that calls the same runner, for on-demand runs.

## Safety model (assistive, never autonomous outward)
- Scout never submits an application and never sends a message on Alex's behalf. It finds, drafts, and shortlists. Alex reviews and submits.
- The only thing Scout sends autonomously is the weekly digest, and that goes only to Alex.
- Warm-intro messages and stale-app follow-ups are drafted, never sent.
- A single `enabled` flag in config disables the whole agent. A `--dry-run` mode finds and scores but drafts and sends nothing.

## Trust layer (so it never wastes time or embarrasses him)
- Live-link verification (stage 2) before any role is shortlisted. Directly prevents the dead-link and wrong-location waste seen on 2 Jun (Google job IDs that 404'd or pointed at Tel Aviv). Verify also checks the location matches the remote or Melbourne rule.
- Draft self-critique: each generated cover letter passes an adversarial review (too generic? overclaiming, for example PyTorch? likely auto-reject?) before it reaches the shortlist. Drafts that fail are revised or flagged, not surfaced as ready.
- Audit log: every run records what was found, verified, scored, and drafted, with reasons. Transparency for Alex, and raw material for the portfolio write-up.

## Privacy layer (so the repo can be public)
- Split public code from private data. All of Alex's data (CV path, contacts, target comp, filters, profile) lives in a gitignored private config, consistent with the secrets-vault pattern. The code is safe to push to GitHub; the data never is.
- Demo mode: a flag that runs Scout on anonymised sample data (fake profile, fake roles), so Alex can screen-share it in an interview without exposing salary, contacts, or strategy.

## Compounding intelligence (v2, makes it smarter weekly)
- Outcome feedback loop: Scout records the fate of each application (no response, response, interview, rejection, offer) and re-weights its scoring and targeting over time. Integrates with `/win-loss`. Over weeks it learns which companies, role types, and framings convert for Alex.
- Warm-path finder: for each HIGH role, before drafting a cold application, Scout searches Alex's network and summit contacts (and public signals) for an intro path into that company, and drafts the intro. Warm outreach converts far better than cold.

## Consolidation and reach (v2)
- Dream-company watchlist: a priority list (Anthropic first) checked every run, with an immediate alert on any Melbourne or AU-remote match. This folds the existing "Anthropic AU Monitor" RemoteTrigger cron (trig_01NBVWEBqRrG1P8ATg4gyKcE) into Scout and retires the separate job.
- Interview-prep auto-trigger: when a tracker row flips to "interview", Scout generates the company prep pack (research, likely questions, Alex's STAR stories) plus the live-demo plan (demoing Scout itself).

## Scope
- v1 (this weekend): stages 1 to 6 (the loop), the full trust layer, the privacy layer, the launchd schedule, and the `/hunt` manual skill. Shippable, safe, useful.
- v2 (later): compounding intelligence (outcome loop, warm-path finder), the dream-company watchlist fold-in, and the interview-prep trigger. Bolt-ons, not blockers.

## Build notes and open decisions (resolve at build time)
- Headless LLM execution: stages discover, verify, score, and draft need an LLM with web access (WebSearch, WebFetch) running unattended on a schedule. Two candidate approaches:
  1. Claude Agent SDK: a Python program that drives a Claude agent with web and file tools. Cleaner, more controllable, the better long-term shape, and itself a stronger portfolio artifact.
  2. `claude -p` headless: invoke the Claude Code CLI non-interactively from the launchd script. Faster to stand up, reuses the existing OS, less code.
  Recommendation: prototype the discover stage with `claude -p` to validate the loop fast, then move the core to the Agent SDK for v1.
- Source list: start with the 2 Jun target set (Anthropic, OpenAI, Google Cloud, Glean, Canva, Databricks, Nash, Relevance AI, Leonardo, Harrison, Lyrebird, Cohere) plus AU AI-native boards. Keep it in config so it stays editable.
- State + tracker: append to the existing `~/Desktop/resumes/Job_Application_Tracker.md` section, or migrate to a small SQLite table for cleaner state. SQLite is the better long-term home and matches the `agency.db` pattern. Decide at build time.

## Testing
- Unit tests for the pure-logic units: filter_dedupe (constraint and dedupe correctness), score (deterministic scoring on fixtures), config parsing, state (idempotency, so re-running yields no duplicates).
- A fixture-based smoke test for the full runner in `--dry-run`.
- discover, verify, and draft are LLM and web bound: test against recorded fixtures, not live, in CI.

## Success criteria
- A weekly run produces a digest with a correct, deduped, verified shortlist and ready-to-review drafts, with zero dead or wrong-location roles.
- Scout never performs an outward action without Alex's click.
- The private config keeps all PII out of the public repo.
- Within a few weeks, the audit log plus a short README make a credible, demo-able portfolio artifact.
