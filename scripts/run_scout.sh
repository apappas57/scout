#!/bin/bash
# run_scout.sh -- launchd heartbeat wrapper for the weekly Scout run.
#
# Mirrors the agency-engine run_job.sh pattern: run the job, capture the real
# exit code, write a heartbeat, and fire a DURABLE ntfy alert on any non-zero
# exit. This catches crashes that happen before the Python reaches its own error
# handling (import error, venv break, OOM), which an in-process notifier cannot.
#
# No secrets live in this script. The ntfy topic is read at runtime from the
# gitignored private config (config/scout.config.toml), never hard-coded here.
#
# Never masks the job: always exits with the job's real exit code.
set +e

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV_PY="$REPO_DIR/.venv/bin/python"
HB_FILE="$REPO_DIR/state/run_scout.heartbeat"
LOG_DIR="$REPO_DIR/logs"
mkdir -p "$REPO_DIR/state" "$LOG_DIR" 2>/dev/null

START_EPOCH=$(date +%s)
START_ISO=$(date '+%Y-%m-%dT%H:%M:%S')

# Run the weekly loop. cd into the repo so relative config and db paths resolve.
cd "$REPO_DIR" || exit 1
"$VENV_PY" -m scout.cli run >> "$LOG_DIR/run_scout.log" 2>&1
RC=$?

END_ISO=$(date '+%Y-%m-%dT%H:%M:%S')
DUR=$(( $(date +%s) - START_EPOCH ))

# Heartbeat: pure printf, no Python dependency, so it survives a Python break.
printf '{"job":"scout-weekly","started_at":"%s","finished_at":"%s","exit_code":%d,"duration_s":%d}\n' \
  "$START_ISO" "$END_ISO" "$RC" "$DUR" > "$HB_FILE" 2>/dev/null

# Yield assertion. An exit code is a claim, not evidence.
#
# Scout catches discovery errors internally and exits 0, so gating the alert on
# $RC alone meant 7 consecutive failed runs (21 Jun to 2 Aug) reported healthy
# while writing zero rows. This asserts a unit of work actually landed, the same
# pattern agency-engine's health_registry uses: assert against the data, and let
# that override the exit code.
ROLES_BEFORE_WINDOW=$("$VENV_PY" - <<'PY' 2>/dev/null
import sqlite3, pathlib
try:
    con = sqlite3.connect("scout.db")
    # rows first seen in the last 2 days = this run's yield
    n = con.execute(
        "select count(*) from roles where first_seen_at >= datetime('now','-2 days')"
    ).fetchone()[0]
    print(n)
except Exception:
    print("-1")
PY
)
[ -z "$ROLES_BEFORE_WINDOW" ] && ROLES_BEFORE_WINDOW=-1

ZERO_YIELD=0
if [ "$ROLES_BEFORE_WINDOW" = "0" ]; then
  ZERO_YIELD=1
fi

# Alert on a real failure OR on a clean-exit-but-no-work run.
if [ "$RC" -ne 0 ] || [ "$ZERO_YIELD" -eq 1 ]; then
  TOPIC="$("$VENV_PY" - <<'PY' 2>/dev/null
import tomllib, pathlib
p = pathlib.Path("config/scout.config.toml")
try:
    cfg = tomllib.loads(p.read_text())
    print(cfg.get("notify", {}).get("ntfy_topic", ""))
except Exception:
    print("")
PY
)"
  if [ -n "$TOPIC" ]; then
    if [ "$RC" -ne 0 ]; then
      ALERT_TITLE="Scout weekly run FAILED"
      ALERT_BODY="exit $RC after ${DUR}s. See logs/run_scout.log."
    else
      ALERT_TITLE="Scout ran clean but found NOTHING"
      ALERT_BODY="exit 0 after ${DUR}s, zero new roles written. Exit code says healthy, the data says it did no work. See logs/run_scout.log."
    fi
    curl -fsS \
      -H "Title: $ALERT_TITLE" \
      -H "Priority: high" \
      -H "Tags: rotating_light" \
      -d "$ALERT_BODY" \
      "https://ntfy.sh/$TOPIC" >/dev/null 2>&1 || true
  fi
fi

exit $RC
