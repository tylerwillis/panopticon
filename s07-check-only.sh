# S07 CHECK ONLY — Check block extracted verbatim from the runbook.
# The Action (claim release) already ran and produced the container this measures.
# Deviation recorded in followups.md: S07's Check races the respawn its Action triggers,
# so the Action is not repeated here (repeating it would restart the same race).
# SOURCE this file; do not execute it.

: "${CANARY_CONTAINER:=panopticon-$CANARY_TASK_ID}"; export CANARY_CONTAINER

_cutover_check() {
  setopt local_options err_return
export CANARY_DEADLINE="$(( $(date +%s) + 300 ))"
until test "$(docker inspect --format '{{.State.Running}}' "$CANARY_CONTAINER" 2>/dev/null)" = true; do
  test "$(date +%s)" -lt "$CANARY_DEADLINE"
  sleep 1
done
export CANARY_CONTAINER_ID="$(docker inspect --format '{{.Id}}' "$CANARY_CONTAINER")"
export CANARY_CONTAINER_STARTED="$(docker inspect --format '{{.State.StartedAt}}' "$CANARY_CONTAINER_ID")"
uv --directory "$APP_ROOT" run python -m panopticon.core.cutover_runbook assert-fresh-container "$CANARY_CONTAINER_ID" "$EVIDENCE_DIR/S01-all-container-ids-before.txt"
uv --directory "$APP_ROOT" run python -m panopticon.core.cutover_runbook assert-fresh-container "$CANARY_CONTAINER_ID" "$EVIDENCE_DIR/S03-all-container-ids-before-enforcement.txt"
uv --directory "$APP_ROOT" run python -m panopticon.core.cutover_runbook assert-container-started-after "$CANARY_CONTAINER_STARTED" "$ENFORCEMENT_STARTED_AT"
docker inspect --format '{{.Id}} {{.State.Pid}} {{.State.StartedAt}}' "$CANARY_CONTAINER_ID" | tee "$EVIDENCE_DIR/S07-container-initial.txt"
docker inspect --format '{{.Id}} {{.State.Pid}} {{.State.StartedAt}}' "$CANARY_CONTAINER_ID" | tee "$EVIDENCE_DIR/S07-container-at-capability.txt"
cmp "$EVIDENCE_DIR/S07-container-initial.txt" "$EVIDENCE_DIR/S07-container-at-capability.txt"
test "$(docker exec "$CANARY_CONTAINER_ID" python -c 'import json; print(json.load(open("/run/secrets/panopticon-service-auth"))["task"][:5])')" = ptc1.
curl --silent --show-error --fail --header "Authorization: Bearer $WRITE_TOKEN" "$SERVICE_URL/tasks/$CANARY_TASK_ID" --output "$EVIDENCE_DIR/S07-live-initial.json"
sleep 6
docker inspect --format '{{.Id}} {{.State.Pid}} {{.State.StartedAt}}' "$CANARY_CONTAINER_ID" | tee "$EVIDENCE_DIR/S07-container-after-keepalive.txt"
cmp "$EVIDENCE_DIR/S07-container-initial.txt" "$EVIDENCE_DIR/S07-container-after-keepalive.txt"
curl --silent --show-error --fail --header "Authorization: Bearer $WRITE_TOKEN" "$SERVICE_URL/tasks/$CANARY_TASK_ID" --output "$EVIDENCE_DIR/S07-live-after-keepalive.json"
uv --directory "$APP_ROOT" run python - "$EVIDENCE_DIR/S07-live-initial.json" "$EVIDENCE_DIR/S07-live-after-keepalive.json" <<'PY'
import json, sys
assert all(json.load(open(path))["container_status"] == "live" for path in sys.argv[1:])
PY
docker inspect --format '{{.Id}} {{.State.Pid}} {{.State.StartedAt}}' "$CANARY_CONTAINER_ID" | tee "$EVIDENCE_DIR/G09-target-at-capability.txt"
cmp "$EVIDENCE_DIR/S07-container-initial.txt" "$EVIDENCE_DIR/G09-target-at-capability.txt"
test "$(docker exec "$CANARY_CONTAINER_ID" python -c 'import json; print(json.load(open("/run/secrets/panopticon-service-auth"))["task"][:5])')" = ptc1.
uv --directory "$APP_ROOT" run python -m panopticon.core.cutover_runbook assert-fresh-container "$CANARY_CONTAINER_ID" "$EVIDENCE_DIR/S03-all-container-ids-before-enforcement.txt"
uv --directory "$APP_ROOT" run python -m panopticon.core.cutover_runbook assert-container-started-after "$CANARY_CONTAINER_STARTED" "$ENFORCEMENT_STARTED_AT"
uv --directory "$APP_ROOT" run python - "$EVIDENCE_DIR/S07-live-initial.json" "$EVIDENCE_DIR/S07-live-after-keepalive.json" <<'PY'
import json, sys
assert all(json.load(open(path))["container_status"] == "live" for path in sys.argv[1:])
PY
cmp "$EVIDENCE_DIR/S07-container-initial.txt" "$EVIDENCE_DIR/S07-container-after-keepalive.txt"
cmp "$EVIDENCE_DIR/G09-target-at-capability.txt" "$EVIDENCE_DIR/S07-container-after-keepalive.txt"
printf 'G09: PASS\n' >> "$EVIDENCE_DIR/gates.txt"
}
print 'S07: running Check (only)...'
_cutover_check
_rc=$?; unset -f _cutover_check
[ $_rc -eq 0 ] || { print -u2 'S07 CHECK FAILED at exit '$_rc; return $_rc; }
print 'S07 CHECK OK — G09 recorded'
