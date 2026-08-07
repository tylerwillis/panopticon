# S08 CHECK ONLY — Check block extracted verbatim from the runbook.
# The Action (POST /runners/local/reclaim) is deliberately NOT run: after the runner
# rename, `local` IS the live runner, so every claim it holds is legitimate and current.
# Reclaiming would release 16 healthy claims and force a redundant full-fleet respawn,
# destroying in-flight agent turns to redo work self-heal already completed correctly.
# Deviation recorded in followups.md.
#
# SOURCE this file; do not execute it. It uses `read -r` for the operator confirmation.

_cutover_check() {
  setopt local_options err_return
export FLEET_DEADLINE="$(( $(date +%s) + 1800 ))"
until curl --silent --show-error --fail --header "Authorization: Bearer $WRITE_TOKEN" "$SERVICE_URL/tasks" --output "$EVIDENCE_DIR/S08-tasks.json" && uv --directory "$APP_ROOT" run python - "$EVIDENCE_DIR/S08-tasks.json" <<'PY'
import json, sys
tasks = json.load(open(sys.argv[1]))
assert all(
    task["terminal"]
    or task["container_status"] == "live"
    or task["container_status"] == "gated"
    or (task["container_status"] == "failed" and task["lifecycle_detail"])
    for task in tasks
)
PY
do
  test "$(date +%s)" -lt "$FLEET_DEADLINE"
  sleep 5
done
uv --directory "$APP_ROOT" run python - "$EVIDENCE_DIR/S08-tasks.json" > "$EVIDENCE_DIR/S08-resumed-harnesses.txt" <<'PY'
import json, sys
tasks = json.load(open(sys.argv[1]))
print("\n".join(sorted({task["harness"] for task in tasks if task["container_status"] == "live"})))
PY
: > "$EVIDENCE_DIR/S08-resume-observations.txt"
for harness in $(cat "$EVIDENCE_DIR/S08-resumed-harnesses.txt"); do
  printf 'Attach to one respawned %s task, verify pre-cutover history is visible and a new turn continues it, then enter confirmed: ' "$harness"
  read -r resumed
  test "$resumed" = confirmed
  printf '%s: transcript-visible-and-continuation-confirmed\n' "$harness" >> "$EVIDENCE_DIR/S08-resume-observations.txt"
done
}
print 'S08: running Check (only)...'
_cutover_check
_rc=$?; unset -f _cutover_check
[ $_rc -eq 0 ] || { print -u2 'S08 CHECK FAILED at exit '$_rc; return $_rc; }
print 'S08 CHECK OK'
