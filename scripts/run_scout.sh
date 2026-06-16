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

# On failure, read the ntfy topic from the gitignored config and fire a durable
# alert. The topic stays out of this file. If no topic is set, stay silent.
if [ "$RC" -ne 0 ]; then
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
    curl -fsS \
      -H "Title: Scout weekly run FAILED" \
      -H "Priority: high" \
      -H "Tags: rotating_light" \
      -d "exit $RC after ${DUR}s. See logs/run_scout.log." \
      "https://ntfy.sh/$TOPIC" >/dev/null 2>&1 || true
  fi
fi

exit $RC
