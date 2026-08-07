# S02 — Reconcile every long-lived client and capture the drain set
#
# SOURCE this:   source ./s02-cutover.sh
# Requires the variables exported by s00 and s01 in this same shell.
#
# Non-destructive. This step only reconciles what S01 inventoried and records
# the exact set of containers S03 will stop. Nothing is killed here.
#
# Deviation from the runbook: no `set -Eeuo pipefail`; failures report and
# return instead of closing your terminal.

_s02_fail() { print -u2 "S02 FAILED: $1"; print -u2 "Failure action: STOP. A freshly launched CLI proves nothing about an original survivor; complete the inventory and disposition evidence first."; return 1; }

[ -n "${EVIDENCE_DIR:-}" ] || { print -u2 "EVIDENCE_DIR not set — source s00-cutover.sh first"; return 1; }
[ -n "${OLD_RUNNER_PID:-}" ] || { print -u2 "OLD_RUNNER_PID not set — source s01-cutover.sh first"; return 1; }

# Every pane inventoried in S01 must be either a known host process (service,
# runner, dashboard — all stopped) or a task container in the drain set. An
# "uncontrolled pane" means something credential-bearing survived that nobody
# has accounted for, which is exactly what G11 exists to catch.
uv --directory "$APP_ROOT" run python - \
     "$EVIDENCE_DIR/S01-panes-before.txt" \
     "$EVIDENCE_DIR/S01-containers-before.txt" \
     "$EVIDENCE_DIR/S02-client-dispositions.txt" <<'PY' || { _s02_fail "an inventoried pane could not be accounted for"; return 1; }
import sys
panes_path, containers_path, output_path = sys.argv[1:]
container_sessions = {
    line.split()[1].removeprefix("/")
    for line in open(containers_path)
    if line.strip()
}
allowed_hosts = {"service", "runner", "dashboard"}
dispositions = []
for line in open(panes_path):
    session, pid = line.split()
    assert session in allowed_hosts or session in container_sessions, f"uncontrolled pane: {session}"
    disposition = "host-process-stopped" if session in allowed_hosts else "task-container-in-drain-set"
    dispositions.append(f"{session} {pid} {disposition}")
open(output_path, "w").write("\n".join(dispositions) + "\n")
PY

# The exact set S03 will stop.
export TASK_CONTAINERS="$(docker ps --quiet --filter label=panopticon.task)"
printf '%s\n' $TASK_CONTAINERS | sed '/^$/d' | tee "$EVIDENCE_DIR/S02-drain-set.txt"

# --- Check block --------------------------------------------------------------
[ "$(wc -l < "$EVIDENCE_DIR/S02-client-dispositions.txt")" -eq "$(wc -l < "$EVIDENCE_DIR/S01-panes-before.txt")" ] \
  || _s02_fail "disposition count does not match the inventoried pane count"

if kill -0 "$OLD_RUNNER_PID" 2>/dev/null; then
  [ "$(ps -o lstart= -p "$OLD_RUNNER_PID" | sed 's/^ *//')" != "$OLD_RUNNER_START" ] \
    || _s02_fail "old runner PID $OLD_RUNNER_PID is still the same process"
fi
if kill -0 "$OLD_DASHBOARD_PID" 2>/dev/null; then
  [ "$(ps -o lstart= -p "$OLD_DASHBOARD_PID" | sed 's/^ *//')" != "$OLD_DASHBOARD_START" ] \
    || _s02_fail "old dashboard PID $OLD_DASHBOARD_PID is still the same process"
fi

print "S02 OK"
print "  panes reconciled: $(wc -l < "$EVIDENCE_DIR/S02-client-dispositions.txt" | tr -d ' ')"
print "  drain set:        $(wc -l < "$EVIDENCE_DIR/S02-drain-set.txt" | tr -d ' ') containers"
print ""
print "  NEXT IS S03 — the irreversible step. It stops every container in the"
print "  drain set. Their pt1 credentials will not work against the new code."
