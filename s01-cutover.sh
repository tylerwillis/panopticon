# S01 — Inventory identities, freeze new work, and wait for stopping points
#
# SOURCE this:   source ./s01-cutover.sh
# Requires the variables exported by s00-cutover.sh in this same shell.
#
# THIS STEP IS DISRUPTIVE. It kills the dashboard, kills the runner, waits for
# every in-flight turn to reach a stopping point, then STOPS THE TASK SERVICE.
# Containers keep running but nothing manages them and the API goes down.
#
# Deviation from the runbook: no `set -Eeuo pipefail`, and the timeout tests are
# explicit `if` guards rather than bare `test` lines that rely on errexit to
# break the loop. Same semantics, but a failure reports and returns instead of
# closing your terminal and destroying EVIDENCE_DIR.

_s01_fail() { print -u2 "S01 FAILED: $1"; print -u2 "Failure action: STOP. Keep every stopped process down and reconcile its original PID/start-time pair before going further."; return 1; }

[ -n "${EVIDENCE_DIR:-}" ] || { print -u2 "EVIDENCE_DIR not set — source s00-cutover.sh first"; return 1; }
[ -n "${PRE_CUTOVER_WRITE_TOKEN:-}" ] || { print -u2 "PRE_CUTOVER_WRITE_TOKEN not set — source s00-cutover.sh first"; return 1; }

# --- inventory every task container ------------------------------------------
export TASK_CONTAINERS_BEFORE="$(docker ps --quiet --no-trunc --filter label=panopticon.task)"
docker ps --all --quiet --no-trunc --filter label=panopticon.task | tee "$EVIDENCE_DIR/S01-all-container-ids-before.txt"
: > "$EVIDENCE_DIR/S01-containers-before.txt"
for container_id in $(cat "$EVIDENCE_DIR/S01-all-container-ids-before.txt"); do
  test -z "$container_id" || docker inspect --format '{{.Id}} {{.Name}} {{.State.StartedAt}}' "$container_id" >> "$EVIDENCE_DIR/S01-containers-before.txt"
done

# --- inventory the long-lived client identities -------------------------------
tmux -L panopticon list-panes -a -F '#{session_name} #{pane_pid}' | tee "$EVIDENCE_DIR/S01-panes-before.txt"
export OLD_RUNNER_PID="$(tmux -L panopticon list-panes -t runner -F '#{pane_pid}' | sed -n '1p')"
export OLD_DASHBOARD_PID="$(tmux -L panopticon list-panes -t dashboard -F '#{pane_pid}' | sed -n '1p')"
[ -n "$OLD_RUNNER_PID" ] || _s01_fail "no runner pane PID found"
[ -n "$OLD_DASHBOARD_PID" ] || _s01_fail "no dashboard pane PID found"
export OLD_RUNNER_START="$(ps -o lstart= -p "$OLD_RUNNER_PID" | sed 's/^ *//')"
export OLD_DASHBOARD_START="$(ps -o lstart= -p "$OLD_DASHBOARD_PID" | sed 's/^ *//')"
ps -o pid= -o lstart= -p "$OLD_RUNNER_PID,$OLD_DASHBOARD_PID" | tee "$EVIDENCE_DIR/S01-client-identities-before.txt"

print "S01: recorded runner pid=$OLD_RUNNER_PID dashboard pid=$OLD_DASHBOARD_PID"

# --- freeze: no new work can be created or spawned ----------------------------
tmux -L panopticon kill-session -t dashboard
tmux -L panopticon kill-session -t runner
print "S01: dashboard and runner stopped; waiting for the live-runner registry to empty..."

export RUNNER_DRAIN_DEADLINE="$(( $(date +%s) + 120 ))"
while :; do
  curl --silent --show-error --fail --header "Authorization: Bearer $PRE_CUTOVER_WRITE_TOKEN" \
       "$SERVICE_URL/runners" --output "$EVIDENCE_DIR/S01-runners-after-stop.json" \
    || { _s01_fail "could not fetch /runners while draining"; return 1; }
  uv --directory "$APP_ROOT" run python -m panopticon.core.cutover_runbook \
       assert-runner-set "$EVIDENCE_DIR/S01-runners-after-stop.json" && break
  if [ "$(date +%s)" -ge "$RUNNER_DRAIN_DEADLINE" ]; then
    _s01_fail "runner registry did not empty within 120s"; return 1
  fi
  sleep 1
done
print "S01: live-runner registry is empty."

# --- wait for every in-flight turn to reach a stopping point ------------------
print "S01: waiting for quiescence (up to 30 minutes)..."
export QUIESCE_DEADLINE="$(( $(date +%s) + 1800 ))"
while :; do
  curl --silent --show-error --fail \
       --header "Authorization: Bearer $(uv --directory "$APP_ROOT" run python -c 'from panopticon.taskservice.auth import environment_token; print(environment_token())')" \
       "$SERVICE_URL/tasks" --output "$EVIDENCE_DIR/S01-tasks.json" \
    || { _s01_fail "could not fetch /tasks while quiescing"; return 1; }
  uv --directory "$APP_ROOT" run python - "$EVIDENCE_DIR/S01-tasks.json" "$OLD_RUNNER_ID" <<'PY' && break
import json, sys
tasks = json.load(open(sys.argv[1]))
assert all(task["terminal"] or task["turn"] == "user" or task["blocked"] for task in tasks)
assert all(
    task["terminal"]
    or task["container_status"] == "gated"
    or task["claimed_by"] == sys.argv[2]
    for task in tasks
)
PY
  if [ "$(date +%s)" -ge "$QUIESCE_DEADLINE" ]; then
    _s01_fail "tasks did not quiesce within 30 minutes"; return 1
  fi
  sleep 5
done
print "S01: all tasks are at a stopping point."

# --- final runner check, then stop the service --------------------------------
curl --silent --show-error --fail --header "Authorization: Bearer $PRE_CUTOVER_WRITE_TOKEN" \
     "$SERVICE_URL/runners" --output "$EVIDENCE_DIR/S01-runners-immediately-before-service-stop.json" \
  || { _s01_fail "could not fetch /runners before stopping the service"; return 1; }
uv --directory "$APP_ROOT" run python -m panopticon.core.cutover_runbook \
     assert-runner-set "$EVIDENCE_DIR/S01-runners-immediately-before-service-stop.json" \
  || { _s01_fail "a runner was still registered immediately before the service stop"; return 1; }

tmux -L panopticon kill-session -t service
print "S01: task service stopped."

# --- Check block --------------------------------------------------------------
if kill -0 "$OLD_RUNNER_PID" 2>/dev/null; then
  [ "$(ps -o lstart= -p "$OLD_RUNNER_PID" | sed 's/^ *//')" != "$OLD_RUNNER_START" ] \
    || _s01_fail "old runner PID $OLD_RUNNER_PID is still the same process"
fi
if kill -0 "$OLD_DASHBOARD_PID" 2>/dev/null; then
  [ "$(ps -o lstart= -p "$OLD_DASHBOARD_PID" | sed 's/^ *//')" != "$OLD_DASHBOARD_START" ] \
    || _s01_fail "old dashboard PID $OLD_DASHBOARD_PID is still the same process"
fi
uv --directory "$APP_ROOT" run python -m panopticon.core.cutover_runbook assert-process-replaced "$OLD_RUNNER_PID" "$OLD_RUNNER_START" \
  || _s01_fail "runner process was not replaced"
uv --directory "$APP_ROOT" run python -m panopticon.core.cutover_runbook assert-process-replaced "$OLD_DASHBOARD_PID" "$OLD_DASHBOARD_START" \
  || _s01_fail "dashboard process was not replaced"
[ -n "$OLD_RUNNER_START" ] || _s01_fail "OLD_RUNNER_START empty"
[ -n "$OLD_DASHBOARD_START" ] || _s01_fail "OLD_DASHBOARD_START empty"
curl --silent --show-error --fail --max-time 2 "$SERVICE_URL/healthz" >/dev/null 2>&1 \
  && _s01_fail "the task service is still answering /healthz"

print "S01 OK — service down, fleet frozen, identities recorded."
print "  OLD_RUNNER_PID=$OLD_RUNNER_PID  OLD_DASHBOARD_PID=$OLD_DASHBOARD_PID"
print "  containers still running: $(docker ps --quiet --filter label=panopticon.task | wc -l | tr -d ' ')"
