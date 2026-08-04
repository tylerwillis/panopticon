# Enforced-mode cutover: drain and respawn

This is a one-way production procedure. It does not accept legacy `pt1` credentials, modify
credentials inside running containers, or weaken PR #163. Issue #202's capability-escape fix must
already be deployed and green. Keep the shell used for this procedure open: later steps reuse the
variables and evidence directory created in S00.

The decisive pre-restart signal is the empty `docker ps` result in S03/G08. The permissive request
counter is only weak corroboration because it cannot see legacy authenticated callers or already
open streams.

## S00 — Prepare evidence, credentials, and prerequisite

### Action

```sh
set -euo pipefail
export APP_ROOT=/path/to/panopticon
export SERVICE_URL=http://127.0.0.1:8000
export PANOPTICON_CONFIG=/path/to/panopticon-config
export AUTH_FILE_NAME=service-auth.json
export AUTH_PATH="$PANOPTICON_CONFIG/secrets/$AUTH_FILE_NAME"
export PANOPTICON_SERVICE_AUTH_FILE="$AUTH_FILE_NAME"
export PWA_ORIGIN=https://phone.example:443
export PHONE_BOARD_URL=https://phone.example:443
export ISSUE_202_COMMIT=replace-with-closing-commit
export DEPLOY_REV="$(git -C "$APP_ROOT" rev-parse HEAD)"
export OLD_RUNNER_ID=local
export NEW_RUNNER_ID="cutover-$(date +%s)"
export EVIDENCE_DIR="$(mktemp -d)"
: > "$EVIDENCE_DIR/followups.md"
git -C "$APP_ROOT" merge-base --is-ancestor "$ISSUE_202_COMMIT" "$DEPLOY_REV"
gh run list --repo tylerwillis/panopticon --commit "$ISSUE_202_COMMIT" --workflow ci.yml --status success --limit 1 --json databaseId --jq 'length == 1' | grep --fixed-strings true
uv --directory "$APP_ROOT" run python -m panopticon.core.cutover_runbook inspect-credential-file "$AUTH_PATH"
```

### Check

```sh
test "$ISSUE_202_COMMIT" != replace-with-closing-commit
test -d "$EVIDENCE_DIR"
test "$NEW_RUNNER_ID" != "$OLD_RUNNER_ID"
test "$AUTH_FILE_NAME" = "$(basename "$AUTH_FILE_NAME")"
uv --directory "$APP_ROOT" run python - "$PWA_ORIGIN" <<'PY'
import sys
from urllib.parse import urlsplit
value = sys.argv[1]
parsed = urlsplit(value)
assert parsed.scheme in {"http", "https"} and parsed.hostname and parsed.port
assert not parsed.username and not parsed.password
assert parsed.path == "" and not parsed.query and not parsed.fragment
assert value == f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"
PY
```

### Expected

The issue #202 closing commit is an ancestor of the deployed revision, its repository CI is green,
the credential file passes the nonprinting safety check, and the replacement runner has a distinct
identity.

### Failure action

STOP before quiescence. Do not infer that an open issue, branch, or unrelated green run satisfies
the prerequisite.

### Evidence status

Production-only: the deployed SHA, workflow run, credential metadata, and chosen runner IDs must be
recorded on the cutover host.

## S01 — Inventory identities, freeze new work, and wait for stopping points

### Action

```sh
export TASK_CONTAINERS_BEFORE="$(docker ps --quiet --filter label=panopticon.task)"
test -z "$TASK_CONTAINERS_BEFORE" || docker inspect --format '{{.Id}} {{.Name}} {{.State.StartedAt}}' $TASK_CONTAINERS_BEFORE | tee "$EVIDENCE_DIR/S01-containers-before.txt"
test -n "$TASK_CONTAINERS_BEFORE" || : > "$EVIDENCE_DIR/S01-containers-before.txt"
tmux -L panopticon list-panes -a -F '#{session_name} #{pane_pid}' | tee "$EVIDENCE_DIR/S01-panes-before.txt"
export OLD_RUNNER_PID="$(awk '$1 == "runner" {print $2}' "$EVIDENCE_DIR/S01-panes-before.txt")"
export OLD_DASHBOARD_PID="$(awk '$1 == "dashboard" {print $2}' "$EVIDENCE_DIR/S01-panes-before.txt")"
export OLD_RUNNER_START="$(ps -o lstart= -p "$OLD_RUNNER_PID" | sed 's/^ *//')"
export OLD_DASHBOARD_START="$(ps -o lstart= -p "$OLD_DASHBOARD_PID" | sed 's/^ *//')"
ps -o pid= -o lstart= -p "$OLD_RUNNER_PID,$OLD_DASHBOARD_PID" | tee "$EVIDENCE_DIR/S01-client-identities-before.txt"
tmux -L panopticon kill-session -t dashboard
tmux -L panopticon kill-session -t runner
while :; do
  curl --silent --show-error --fail --header "Authorization: Bearer $(uv --directory "$APP_ROOT" run python -c 'from panopticon.taskservice.auth import environment_token; print(environment_token())')" "$SERVICE_URL/tasks" --output "$EVIDENCE_DIR/S01-tasks.json"
  uv --directory "$APP_ROOT" run python - "$EVIDENCE_DIR/S01-tasks.json" <<'PY' && break
import json, sys
tasks = json.load(open(sys.argv[1]))
terminal = {"COMPLETE", "DROPPED"}
assert all(task["state"] in terminal or task["turn"] == "user" or task["blocked"] for task in tasks)
PY
  sleep 5
done
tmux -L panopticon kill-session -t service
```

### Check

```sh
! kill -0 "$OLD_RUNNER_PID" 2>/dev/null || test "$(ps -o lstart= -p "$OLD_RUNNER_PID" | sed 's/^ *//')" != "$OLD_RUNNER_START"
! kill -0 "$OLD_DASHBOARD_PID" 2>/dev/null || test "$(ps -o lstart= -p "$OLD_DASHBOARD_PID" | sed 's/^ *//')" != "$OLD_DASHBOARD_START"
test -n "$OLD_RUNNER_START"
test -n "$OLD_DASHBOARD_START"
! curl --silent --show-error --fail --max-time 2 "$SERVICE_URL/healthz"
```

### Expected

The inventory records PID and start time for the original runner and dashboard. Creation through
the dashboard and spawning/resume through the runner are frozen before waiting; all in-flight turns
reach a recorded user-turn, blocked, or terminal stopping point; then the service stops accepting
API work.

### Failure action

STOP. Keep every stopped process down and reconcile its original PID/start-time pair before going
further.

### Evidence status

Production-only: process identities and stopping-point task state cannot be proven while authoring.

## S02 — Reconcile every long-lived client and capture the drain set

### Action

```sh
awk '$1 == "runner" || $1 == "dashboard" {print}' "$EVIDENCE_DIR/S01-panes-before.txt" | tee "$EVIDENCE_DIR/S02-client-dispositions.txt"
export TASK_CONTAINERS="$(docker ps --quiet --filter label=panopticon.task)"
printf '%s\n' $TASK_CONTAINERS | sed '/^$/d' | tee "$EVIDENCE_DIR/S02-drain-set.txt"
```

### Check

```sh
test "$(wc -l < "$EVIDENCE_DIR/S02-client-dispositions.txt")" -eq 2
! kill -0 "$OLD_RUNNER_PID" 2>/dev/null || test "$(ps -o lstart= -p "$OLD_RUNNER_PID" | sed 's/^ *//')" != "$OLD_RUNNER_START"
! kill -0 "$OLD_DASHBOARD_PID" 2>/dev/null || test "$(ps -o lstart= -p "$OLD_DASHBOARD_PID" | sed 's/^ *//')" != "$OLD_DASHBOARD_START"
```

### Expected

Every inventoried credential-bearing survivor has an original PID/start-time identity and a
confirmed-dead disposition. The exact running-container drain set is recorded.

### Failure action

STOP. A freshly launched CLI proves nothing about an original survivor; complete the inventory and
disposition evidence first.

### Evidence status

Production-only: survivor disposition and the live drain set are host observations.

## S03 — Stop every task container and prove an empty fleet

### Action

```sh
test -z "$TASK_CONTAINERS" || docker stop $TASK_CONTAINERS
```

### Check

```sh
docker ps --quiet --filter label=panopticon.task | tee "$EVIDENCE_DIR/S03-running-after.txt"
test ! -s "$EVIDENCE_DIR/S03-running-after.txt"
```

### Expected

Expected output: empty. Zero running task containers is the direct enforcement gate; the
permissive counter is never a gate.

### Failure action

STOP on any nonzero drain. Do not start the service or replacement clients.

### Evidence status

Production-only: this step deliberately stops the production fleet.

## S04 — Start replacement runner and waiting dashboard with fresh identities

### Action

```sh
tmux -L panopticon set-environment -g PANOPTICON_SERVICE_AUTH_FILE "$AUTH_FILE_NAME"
tmux -L panopticon set-environment -g PANOPTICON_SERVICE_AUTH_MODE enforced
tmux -L panopticon set-environment -g PANOPTICON_CONFIG "$PANOPTICON_CONFIG"
tmux -L panopticon set-environment -g PANOPTICON_BROWSER_ORIGINS "$PWA_ORIGIN"
tmux -L panopticon new-session -d -s runner -c "$APP_ROOT" "env PANOPTICON_CONFIG='$PANOPTICON_CONFIG' PANOPTICON_SERVICE_AUTH_FILE='$AUTH_FILE_NAME' PANOPTICON_SERVICE_AUTH_MODE=enforced PANOPTICON_RUNNER_ID='$NEW_RUNNER_ID' uv run python -m panopticon.sessionservice.host"
tmux -L panopticon new-session -d -s dashboard -c "$APP_ROOT" "until curl --silent --fail '$SERVICE_URL/healthz' >/dev/null; do sleep 1; done; exec env PANOPTICON_CONFIG='$PANOPTICON_CONFIG' PANOPTICON_SERVICE_AUTH_FILE='$AUTH_FILE_NAME' PANOPTICON_SERVICE_AUTH_MODE=enforced PANOPTICON_BROWSER_ORIGINS='$PWA_ORIGIN' uv run panopticon --service-url '$SERVICE_URL' dashboard"
export NEW_RUNNER_PID="$(tmux -L panopticon display-message -p -t runner '#{pane_pid}')"
export NEW_DASHBOARD_PID="$(tmux -L panopticon display-message -p -t dashboard '#{pane_pid}')"
export NEW_RUNNER_START="$(ps -o lstart= -p "$NEW_RUNNER_PID" | sed 's/^ *//')"
export NEW_DASHBOARD_START="$(ps -o lstart= -p "$NEW_DASHBOARD_PID" | sed 's/^ *//')"
ps -o pid= -o lstart= -p "$NEW_RUNNER_PID,$NEW_DASHBOARD_PID" | tee "$EVIDENCE_DIR/S04-client-identities-after.txt"
```

### Check

```sh
test "$NEW_RUNNER_PID" != "$OLD_RUNNER_PID"
test "$NEW_RUNNER_START" != "$OLD_RUNNER_START" || test "$NEW_RUNNER_PID" != "$OLD_RUNNER_PID"
test "$NEW_DASHBOARD_PID" != "$OLD_DASHBOARD_PID"
test "$NEW_DASHBOARD_START" != "$OLD_DASHBOARD_START" || test "$NEW_DASHBOARD_PID" != "$OLD_DASHBOARD_PID"
test -n "$NEW_RUNNER_START"
test -n "$NEW_DASHBOARD_START"
kill -0 "$NEW_RUNNER_PID"
kill -0 "$NEW_DASHBOARD_PID"
docker ps --quiet --filter label=panopticon.task | tee "$EVIDENCE_DIR/G08-running.txt"
test ! -s "$EVIDENCE_DIR/G08-running.txt"
printf 'G08: PASS\n' >> "$EVIDENCE_DIR/gates.txt"
```

### Expected

Both replacement PID/start-time pairs differ from the original pairs. The distinct replacement
runner ID prevents it from healing tasks still claimed by the old runner before the canary gate.

### Failure action

STOP. Kill both replacement sessions and repeat the identity comparison.

### Evidence status

Production-only: replacement identity is observable only on the cutover host.

## S05 — Start the task service with exported enforced configuration

### Action

```sh
export PANOPTICON_SERVICE_AUTH_MODE=enforced
export PANOPTICON_SERVICE_AUTH_FILE="$AUTH_FILE_NAME"
export PANOPTICON_BROWSER_ORIGINS="$PWA_ORIGIN"
tmux -L panopticon new-session -d -s service -c "$APP_ROOT" "exec env PANOPTICON_CONFIG='$PANOPTICON_CONFIG' PANOPTICON_SERVICE_AUTH_MODE='$PANOPTICON_SERVICE_AUTH_MODE' PANOPTICON_SERVICE_AUTH_FILE='$PANOPTICON_SERVICE_AUTH_FILE' PANOPTICON_BROWSER_ORIGINS='$PANOPTICON_BROWSER_ORIGINS' uv run python -m panopticon.taskservice"
```

### Check

```sh
until curl --silent --show-error --fail "$SERVICE_URL/healthz"; do sleep 1; done
tmux -L panopticon show-environment -g PANOPTICON_BROWSER_ORIGINS | grep --fixed-strings "PANOPTICON_BROWSER_ORIGINS=$PWA_ORIGIN"
```

### Expected

The child process inherits enforced mode, the credential filename beneath the configured secrets
directory, and the exact scheme-host-port browser origin with no path, query, fragment, credentials,
or trailing slash.

### Failure action

ROLL BACK. Keep containers stopped, kill the three replacement sessions, and restore the last
known-good service configuration.

### Evidence status

Production-only: service startup and inherited environment require host evidence.

## S06 — Run gates G01 through G07

### Action

```sh
export WRITE_TOKEN="$(uv --directory "$APP_ROOT" run python -c 'from panopticon.taskservice.auth import environment_token; print(environment_token())')"
export READ_TOKEN="$(uv --directory "$APP_ROOT" run python -c 'from panopticon.taskservice.auth import environment_token; print(environment_token(privilege="read"))')"
```

### Check

```sh
test -n "$WRITE_TOKEN"
test -n "$READ_TOKEN"
```

### Expected

G01 through G07 below pass after G08 has already passed immediately before S05 and before any task
claim is released.

### Failure action

ROLL BACK on the first failed gate.

### Evidence status

Production-only: the gates exercise the deployed service, runner, browser origin, and phone board.

## S07 — Release and verify exactly one real canary

### Action

```sh
curl --silent --show-error --fail --request DELETE --header "Authorization: Bearer $WRITE_TOKEN" "$SERVICE_URL/tasks/$CANARY_TASK_ID/claim" --output "$EVIDENCE_DIR/S07-release.json"
export CANARY_CONTAINER="panopticon-$CANARY_TASK_ID"
```

### Check

```sh
until test "$(docker inspect --format '{{.State.Running}}' "$CANARY_CONTAINER" 2>/dev/null)" = true; do sleep 1; done
docker inspect --format '{{.Id}} {{.State.Pid}} {{.State.StartedAt}}' "$CANARY_CONTAINER" | tee "$EVIDENCE_DIR/S07-container-initial.txt"
test "$(docker exec "$CANARY_CONTAINER" python -c 'import json; print(json.load(open("/run/secrets/panopticon-service-auth"))["task"][:5])')" = ptc1.
curl --silent --show-error --fail --header "Authorization: Bearer $WRITE_TOKEN" "$SERVICE_URL/tasks/$CANARY_TASK_ID" --output "$EVIDENCE_DIR/S07-live-initial.json"
sleep 6
docker inspect --format '{{.Id}} {{.State.Pid}} {{.State.StartedAt}}' "$CANARY_CONTAINER" | tee "$EVIDENCE_DIR/S07-container-after-keepalive.txt"
cmp "$EVIDENCE_DIR/S07-container-initial.txt" "$EVIDENCE_DIR/S07-container-after-keepalive.txt"
curl --silent --show-error --fail --header "Authorization: Bearer $WRITE_TOKEN" "$SERVICE_URL/tasks/$CANARY_TASK_ID" --output "$EVIDENCE_DIR/S07-live-after-keepalive.json"
uv --directory "$APP_ROOT" run python - "$EVIDENCE_DIR/S07-live-initial.json" "$EVIDENCE_DIR/S07-live-after-keepalive.json" <<'PY'
import json, sys
assert all(json.load(open(path))["container_status"] == "live" for path in sys.argv[1:])
PY
```

### Expected

One freshly spawned real container reads `ptc1.` from its mounted capability and remains registered
live across the five-second liveness keepalive interval before bulk respawn.
S07 releases only the canary task claim.

### Failure action

STOP before bulk respawn. Stop the canary and roll back while the remaining tasks stay claimed by
the old runner.

### Evidence status

Production-only: only a real post-enforcement container can prove mounted capability and liveness.

## S08 — Release old-runner claims for controlled bulk respawn

### Action

```sh
curl --silent --show-error --fail --request POST --header "Authorization: Bearer $WRITE_TOKEN" "$SERVICE_URL/runners/$OLD_RUNNER_ID/reclaim" --output "$EVIDENCE_DIR/S08-reclaimed.json"
```

### Check

```sh
until curl --silent --show-error --fail --header "Authorization: Bearer $WRITE_TOKEN" "$SERVICE_URL/tasks" --output "$EVIDENCE_DIR/S08-tasks.json" && uv --directory "$APP_ROOT" run python - "$EVIDENCE_DIR/S08-tasks.json" <<'PY'
import json, sys
tasks = json.load(open(sys.argv[1]))
terminal = {"COMPLETE", "DROPPED"}
assert all(
    task["state"] in terminal
    or task["container_status"] == "live"
    or (task["container_status"] == "failed" and task["lifecycle_detail"])
    for task in tasks
)
PY
do sleep 5; done
```

### Expected

Every intended nonterminal task is live or has a recorded task-specific failed disposition.

### Failure action

STOP further intervention, preserve per-task failure evidence, and do not report fleet success.

### Evidence status

Production-only: full fleet restoration cannot be simulated honestly.

## S09 — Append evidence and follow-ups to issue #203

### Action

```sh
{
  printf 'Enforced-mode cutover evidence for %s\n\n' "$DEPLOY_REV"
  cat "$EVIDENCE_DIR/gates.txt"
  printf '\nNew follow-up work (append to issue #203; do not duplicate #202/#203):\n'
  cat "$EVIDENCE_DIR/followups.md"
  printf '\nEvidence files:\n'
  find "$EVIDENCE_DIR" -maxdepth 1 -type f -printf '%f\n' | sort
} > "$EVIDENCE_DIR/cutover-summary.md"
gh issue comment 203 --repo tylerwillis/panopticon --body-file "$EVIDENCE_DIR/cutover-summary.md"
```

### Check

```sh
gh issue view 203 --repo tylerwillis/panopticon --comments --json comments --jq '.comments[-1].body' | grep --fixed-strings 'G11: PASS'
```

### Expected

Issue #203 contains the recorded step/gate evidence and any newly discovered follow-up work. Do not
open a new issue for anything already described in #202 or #203.

### Failure action

STOP. Preserve the local evidence directory and retry the append without opening duplicate issues.

### Evidence status

Authoring-tested: command syntax and issue target are checked; production evidence remains absent
until the cutover runs.

## Rollback

Trigger rollback on prerequisite failure, nonzero drain, stale-client evidence, service startup,
security/browser gate, or canary failure. Keep every task container stopped; kill the replacement
service, runner, and dashboard sessions; restore the last-known-good service environment; restart
the clients; and repeat S01 identity inventory before releasing any claim. Do not restore legacy
capability acceptance and do not expect a killed PID to reappear.

## The eleven gates

G01–G07 are PR #163's original seven cutover gates. G08–G11 are issue #203's four
adversarial-review additions.

### G01 — Health remains public

#### Command

```sh
test "$(curl --silent --output /dev/null --write-out '%{http_code}' "$SERVICE_URL/healthz")" = 200
```

#### Check

```sh
curl --silent --show-error --fail "$SERVICE_URL/healthz" | grep --fixed-strings '"status":"ok"'
printf 'G01: PASS\n' >> "$EVIDENCE_DIR/gates.txt"
```

#### Expected

Unauthenticated `GET /healthz` returns 200.

#### Failure action

STOP; the service is not healthy.

#### Evidence status

Production-only: deployed health response.

### G02 — Fleet tasks reject missing authentication

#### Command

```sh
test "$(curl --silent --output /dev/null --write-out '%{http_code}' "$SERVICE_URL/tasks")" = 401
```

#### Check

```sh
test -n "$SERVICE_URL"
printf 'G02: PASS\n' >> "$EVIDENCE_DIR/gates.txt"
```

#### Expected

Unauthenticated `GET /tasks` returns 401.

#### Failure action

STOP; enforced authentication is not active.

#### Evidence status

Production-only: deployed authentication behavior.

### G03 — The recorded replacement runner is live

#### Command

```sh
curl --silent --show-error --fail --header "Authorization: Bearer $WRITE_TOKEN" "$SERVICE_URL/runners/$NEW_RUNNER_ID" --output "$EVIDENCE_DIR/G03-runner.json"
```

#### Check

```sh
kill -0 "$NEW_RUNNER_PID"
test "$(ps -o lstart= -p "$NEW_RUNNER_PID" | sed 's/^ *//')" = "$NEW_RUNNER_START"
grep --fixed-strings "\"id\":\"$NEW_RUNNER_ID\"" "$EVIDENCE_DIR/G03-runner.json"
printf 'G03: PASS\n' >> "$EVIDENCE_DIR/gates.txt"
```

#### Expected

The authenticated live-runner API names the runner bound to the recorded new PID/start-time pair.

#### Failure action

STOP; a new CLI command is not substitute evidence.

#### Evidence status

Production-only: active runner process and registration.

### G04 — Read token can read with the exact phone origin

#### Command

```sh
curl --silent --show-error --dump-header "$EVIDENCE_DIR/G04-headers.txt" --output "$EVIDENCE_DIR/G04-tasks.json" --write-out '%{http_code}' --header "Authorization: Bearer $READ_TOKEN" --header "Origin: $PWA_ORIGIN" "$SERVICE_URL/tasks" | tee "$EVIDENCE_DIR/G04-status.txt"
```

#### Check

```sh
test "$(cat "$EVIDENCE_DIR/G04-status.txt")" != 401
test "$(cat "$EVIDENCE_DIR/G04-status.txt")" != 403
grep --ignore-case --fixed-strings "access-control-allow-origin: $PWA_ORIGIN" "$EVIDENCE_DIR/G04-headers.txt"
printf 'G04: PASS\n' >> "$EVIDENCE_DIR/gates.txt"
```

#### Expected

Read-token `GET /tasks` is authorized and echoes the exact origin.

#### Failure action

STOP; phone-board read authority is broken.

#### Evidence status

Production-only: deployed read/CORS behavior.

### G05 — Read token cannot mutate a task

#### Command

```sh
test "$(curl --silent --output /dev/null --write-out '%{http_code}' --request PUT --header 'Content-Type: application/json' --header "Authorization: Bearer $READ_TOKEN" --data '{"turn":"agent"}' "$SERVICE_URL/tasks/$CANARY_TASK_ID/turn")" = 401
```

#### Check

```sh
test -n "$CANARY_TASK_ID"
printf 'G05: PASS\n' >> "$EVIDENCE_DIR/gates.txt"
```

#### Expected

Read-token `PUT /tasks/<canary>/turn` returns 401.

#### Failure action

STOP; the read boundary permits mutation.

#### Evidence status

Production-only: deployed authorization behavior.

### G06 — Preflight and actual CORS responses are exact

#### Command

```sh
curl --silent --show-error --request OPTIONS --dump-header "$EVIDENCE_DIR/G06-preflight.txt" --output /dev/null --header "Origin: $PWA_ORIGIN" --header 'Access-Control-Request-Method: GET' --header 'Access-Control-Request-Headers: Authorization' "$SERVICE_URL/tasks"
curl --silent --show-error --dump-header "$EVIDENCE_DIR/G06-actual.txt" --output /dev/null --header "Authorization: Bearer $READ_TOKEN" --header "Origin: $PWA_ORIGIN" "$SERVICE_URL/tasks"
```

#### Check

```sh
uv --directory "$APP_ROOT" run python - "$PWA_ORIGIN" "$EVIDENCE_DIR/G06-preflight.txt" "$EVIDENCE_DIR/G06-actual.txt" <<'PY'
import sys
origin = sys.argv[1]
for path in sys.argv[2:]:
    headers = {}
    for line in open(path):
        if ":" in line:
            name, value = line.split(":", 1)
            headers.setdefault(name.lower(), []).append(value.strip())
    assert headers.get("access-control-allow-origin") == [origin]
    assert "access-control-allow-credentials" not in headers
PY
printf 'G06: PASS\n' >> "$EVIDENCE_DIR/gates.txt"
```

#### Expected

Both responses echo only the exact origin and omit credential-sharing permission.

#### Failure action

STOP on either response.

#### Evidence status

Production-only: deployed browser boundary.

### G07 — Installed phone board agrees with the authenticated API

#### Command

```sh
curl --silent --show-error --fail "$PHONE_BOARD_URL" --output "$EVIDENCE_DIR/G07-phone.html"
curl --silent --show-error --fail --header "Authorization: Bearer $READ_TOKEN" "$SERVICE_URL/tasks" --output "$EVIDENCE_DIR/G07-api.json"
```

#### Check

```sh
uv --directory "$APP_ROOT" run python - "$KNOWN_TASK_NAME" "$EVIDENCE_DIR/G07-phone.html" "$EVIDENCE_DIR/G07-api.json" <<'PY'
import json, sys
from html.parser import HTMLParser
class Text(HTMLParser):
    def __init__(self): super().__init__(); self.values = []
    def handle_data(self, data): self.values.append(data.strip())
name, html_path, api_path = sys.argv[1:]
phone = Text(); phone.feed(open(html_path).read())
tasks = json.load(open(api_path))
assert name in phone.values
assert any(name in {task.get("name"), task.get("slug")} for task in tasks)
PY
printf 'G07: PASS\n' >> "$EVIDENCE_DIR/gates.txt"
```

#### Expected

Two independent sources display the same chosen task name.

#### Failure action

STOP; installed browser behavior is not proven.

#### Evidence status

Production-only: installed phone board.

### G08 — No task container runs immediately before enforcement

#### Command

```sh
docker ps --quiet --filter label=panopticon.task | tee "$EVIDENCE_DIR/G08-running.txt"
```

#### Check

```sh
test ! -s "$EVIDENCE_DIR/G08-running.txt"
printf 'G08: PASS\n' >> "$EVIDENCE_DIR/gates.txt"
```

#### Expected

Expected output: empty immediately before S05.

#### Failure action

STOP; return to S03.

#### Evidence status

Production-only: direct Docker observation.

### G09 — Real post-enforcement canary has ptc1 capability and liveness

#### Command

```sh
test "$(docker exec "$CANARY_CONTAINER" python -c 'import json; print(json.load(open("/run/secrets/panopticon-service-auth"))["task"][:5])')" = ptc1.
```

#### Check

```sh
uv --directory "$APP_ROOT" run python - "$EVIDENCE_DIR/S07-live-initial.json" "$EVIDENCE_DIR/S07-live-after-keepalive.json" <<'PY'
import json, sys
assert all(json.load(open(path))["container_status"] == "live" for path in sys.argv[1:])
PY
cmp "$EVIDENCE_DIR/S07-container-initial.txt" "$EVIDENCE_DIR/S07-container-after-keepalive.txt"
printf 'G09: PASS\n' >> "$EVIDENCE_DIR/gates.txt"
```

#### Expected

The same real freshly spawned container ID, init PID, and start time reads its mounted `ptc1.`
capability and stays live across a later keepalive.

#### Failure action

STOP before bulk respawn.

#### Evidence status

Production-only: real container and open liveness registration.

### G10 — Runner process identity changed

#### Command

```sh
test "$NEW_RUNNER_PID" != "$OLD_RUNNER_PID"
test "$NEW_RUNNER_START" != "$OLD_RUNNER_START" || test "$NEW_RUNNER_PID" != "$OLD_RUNNER_PID"
```

#### Check

```sh
kill -0 "$NEW_RUNNER_PID"
test "$(ps -o lstart= -p "$NEW_RUNNER_PID" | sed 's/^ *//')" = "$NEW_RUNNER_START"
! kill -0 "$OLD_RUNNER_PID" 2>/dev/null || test "$(ps -o lstart= -p "$OLD_RUNNER_PID" | sed 's/^ *//')" != "$OLD_RUNNER_START"
printf 'G10: PASS\n' >> "$EVIDENCE_DIR/gates.txt"
```

#### Expected

Old and new PID/start-time pairs differ and only the new process exists.

#### Failure action

STOP; repeat S04.

#### Evidence status

Production-only: host process identity.

### G11 — Every original long-lived client is restarted or confirmed dead

#### Command

```sh
test -s "$EVIDENCE_DIR/S01-client-identities-before.txt"
test -s "$EVIDENCE_DIR/S04-client-identities-after.txt"
test -n "$OLD_RUNNER_START"
test -n "$OLD_DASHBOARD_START"
```

#### Check

```sh
! kill -0 "$OLD_RUNNER_PID" 2>/dev/null || test "$(ps -o lstart= -p "$OLD_RUNNER_PID" | sed 's/^ *//')" != "$OLD_RUNNER_START"
! kill -0 "$OLD_DASHBOARD_PID" 2>/dev/null || test "$(ps -o lstart= -p "$OLD_DASHBOARD_PID" | sed 's/^ *//')" != "$OLD_DASHBOARD_START"
kill -0 "$NEW_RUNNER_PID"
kill -0 "$NEW_DASHBOARD_PID"
test "$(ps -o lstart= -p "$NEW_RUNNER_PID" | sed 's/^ *//')" = "$NEW_RUNNER_START"
test "$(ps -o lstart= -p "$NEW_DASHBOARD_PID" | sed 's/^ *//')" = "$NEW_DASHBOARD_START"
printf 'G11: PASS\n' >> "$EVIDENCE_DIR/gates.txt"
```

#### Expected

Every original survivor is confirmed dead by original identity and has a replacement identity.

#### Failure action

STOP; reconcile the complete S01 inventory.

#### Evidence status

Production-only: complete process reconciliation.

## Resume evidence

| Harness | Configuration-volume persistence | Launcher resume selection | Real CLI transcript acceptance |
| --- | --- | --- | --- |
| Claude | Unit: both LocalRunner launches mount the same sole per-task `.claude` volume. | Unit: history selects `--continue` (with the interruption prompt only for an agent turn). | Production-only: observe a resumed real Claude task after cutover. |
| Codex | Unit: both LocalRunner launches mount the same sole per-task `.codex` volume. | Unit: history selects the newest explicit interactive session ID, never `--last`. | Production-only: observe a resumed real Codex task after cutover. |

Real vendor-CLI acceptance of either persisted transcript remains unproven until G09 and a resumed
task are observed during cutover.

## What remains unproven until cutover

Production process identity, credential-file acceptance by the deployed service, network and CORS
behavior, installed-phone behavior, real-container capability mounting, liveness across keepalive,
and vendor CLI transcript acceptance remain unproven until their production evidence files are
recorded. Passing unit tests is not a substitute for those observations.

## Rejected strategies

Do not add a `pt1` compatibility window. Do not replace credentials inside running containers.
Do not revert or weaken PR #163. The distinct replacement runner ID is a temporary ownership bridge,
not a credential compatibility path; after S08 it owns all reclaimed nonterminal work.
