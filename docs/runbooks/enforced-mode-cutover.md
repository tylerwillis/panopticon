# Enforced-mode cutover: drain and respawn

Use this file from the repository checkout while the service is down.
No step after S03 depends on reading this file from the task service. Record every command, result, PID, start time, and failure
disposition in one evidence file. Substitute the deployment-specific values before starting.

This cutover deliberately replaces an unreliable inferred signal with a directly observable fact:
the permissive unauthenticated-request counter is a weak corroborating signal that cannot authorize cutover;
zero running task containers is the enforcement gate. Legacy `pt1` clients are not counted by that
counter. Draining also avoids migration code because spawn-time capability snapshotting supplies a
new scoped capability to every replacement container.

G01–G07: PR #163 deployment gates. G08–G11: issue #203 cutover additions.

## S00 — Verify the issue #202 prerequisite

### Action

```sh
ISSUE_202_COMMIT=<closing-commit>
DEPLOY_REV=<exact-deployed-revision>
REPOSITORY_GATE=ci
git rev-parse "$ISSUE_202_COMMIT" "$DEPLOY_REV"
```

### Check

```sh
git merge-base --is-ancestor "$ISSUE_202_COMMIT" "$DEPLOY_REV"
gh run list --commit "$ISSUE_202_COMMIT" --workflow "$REPOSITORY_GATE" --status success --limit 1
gh run view "$(gh run list --commit "$ISSUE_202_COMMIT" --workflow "$REPOSITORY_GATE" --status success --limit 1 --json databaseId --jq '.[0].databaseId')" --exit-status
```

### Expected

The issue #202 closing commit is an ancestor of the deployed revision, and the matching repository
gate for that same commit is green.

### Failure action

STOP before quiescence. Do not run this cutover until task `8a044631` has landed and its exact closing
commit has a green repository gate.

### Evidence status

Cutover-only: the production revision and matching CI run can only be recorded during cutover.

## S01 — Quiesce new work

### Action

```sh
panopticon-admin quiesce --create --respawn --resume
```

### Check

```sh
panopticon-admin quiesce-status --assert-frozen --wait-recorded-stopping-points
```

### Expected

The action must stop accepting new work; creation, respawn, and resume are frozen before waiting.
All in-flight turns reach a recorded stopping point.

### Failure action

STOP. Keep admission frozen and resolve every turn without starting a new task.

### Evidence status

Cutover-only: quiescence affects the production work queue.

## S02 — Inventory containers and long-lived clients

### Action

```sh
docker ps --filter label=panopticon.task --format '{{.ID}} {{.Names}} {{.RunningFor}}'
TASK_CONTAINERS="$(docker ps --quiet --filter label=panopticon.task)"
tmux -L panopticon list-panes -a -F '#{session_name} #{pane_pid}'
ps -o pid= -o lstart= -p "$RUNNER_PID,$DASHBOARD_PID"
```

### Check

```sh
panopticon-cutover inventory --assert-discovered-equals-recorded --identity-fields pid,start_time
```

### Expected

Every running task container and every credential-bearing long-lived process has one inventory row
containing PID and start time; record `pane_pid` and `ps -o lstart=` evidence for runner and dashboard.

### Failure action

STOP. Expand the inventory until discovered and recorded sets are equal.

### Evidence status

Cutover-only: production process identity cannot be inferred from tests.

## S03 — Stop and directly drain all task containers

### Action

```sh
test -z "$TASK_CONTAINERS" || docker stop $TASK_CONTAINERS
```

### Check

```sh
docker ps --quiet --filter label=panopticon.task
```

### Expected

Expected output: empty. This directly proves zero running task containers.
The permissive request counter is only a weak corroborating signal and is never a gate.

### Failure action

STOP on any nonzero drain. Do not restart the task service or any task container.

### Evidence status

Cutover-only: this command intentionally stops the production task fleet.

## S04 — Replace every credential-bearing long-lived client

### Action

```sh
tmux -L panopticon kill-session -t runner
tmux -L panopticon kill-session -t dashboard
panopticon start
```

### Check

```sh
panopticon-cutover reconcile-clients --inventory "$INVENTORY_FILE" --require-new-pid-and-start
kill -0 "$OLD_RUNNER_PID" 2>/dev/null && exit 1 || true
```

### Expected

Record old PID, old start time, new PID, and new start time. The new PID/start-time pair differs for
the active runner; every survivor is restarted or confirmed dead using its original identity.

### Failure action

STOP. A freshly launched CLI proves nothing about a survivor; reconcile every original process.

### Evidence status

Cutover-only: production process replacement requires before/after identity evidence.

## S05 — Validate credentials and restart the service enforced

### Action

```sh
AUTH_FILE_NAME=service-auth.json
PWA_ORIGIN=https://phone.example:443
PANOPTICON_SERVICE_AUTH_MODE=enforced
PANOPTICON_SERVICE_AUTH_FILE="$AUTH_FILE_NAME"
PANOPTICON_BROWSER_ORIGINS="$PWA_ORIGIN"
tmux -L panopticon set-environment -g PANOPTICON_BROWSER_ORIGINS "$PWA_ORIGIN"
tmux -L panopticon kill-session -t service
panopticon host
```

### Check

The file checks below require a regular file that is not a symbolic link, has mode 0600, and is
owned by the service user. The metadata check requires a nonempty `write` array and a
distinct nonempty `read` array, and it must not print token values.

```sh
test -f "$PANOPTICON_CONFIG/secrets/$AUTH_FILE_NAME"
test ! -L "$PANOPTICON_CONFIG/secrets/$AUTH_FILE_NAME"
test "$(stat --format='%a' "$PANOPTICON_CONFIG/secrets/$AUTH_FILE_NAME")" = 600
test "$(stat --format='%U' "$PANOPTICON_CONFIG/secrets/$AUTH_FILE_NAME")" = "$SERVICE_USER"
python -m panopticon.core.cutover_runbook inspect-credential-file "$PANOPTICON_CONFIG/secrets/$AUTH_FILE_NAME"
tmux -L panopticon list-sessions
```

### Expected

The credential is a regular file, not a symbolic link, has permissions `0600`, and is owned by the service user.
Validation finds populated, disjoint `write` and `read` arrays. The check must not
print token values. `PANOPTICON_SERVICE_AUTH_FILE` is the filename beneath the configured secrets directory.
`PANOPTICON_BROWSER_ORIGINS` is the PWA scheme, host, and port only, with no path, query, fragment,
credentials, or trailing slash. Every already-running supervisor receives the exact origin.

### Failure action

ROLL BACK. Keep containers stopped and restore the last known-good service configuration.

### Evidence status

Cutover-only: production file metadata, service startup, and origin injection require host evidence.

## S06 — Run the first six deployment gates

### Action

```sh
run-gates G01 G02 G03 G04 G05 G06
```

### Check

```sh
test all-gates-passed
```

### Expected

All six gates pass.

### Failure action

STOP and roll back.

### Evidence status

Authoring: the assertions were unit-tested; production HTTP and browser behavior remain cutover-only.

## S07 — Respawn exactly one real canary

### Action

```sh
panopticon-cutover respawn-canary --count 1 --task "$CANARY_TASK_ID"
```

### Check

```sh
run-gate G09 --before-bulk-respawn
```

### Expected

Exactly one real canary passes G09 before bulk respawn.

### Failure action

STOP. Keep the remaining fleet drained and roll back the canary.

### Evidence status

Cutover-only: mounted capability and liveness must be asserted against a real production container.

## S08 — Bulk respawn the intended fleet

### Action

```sh
panopticon-cutover respawn --scope every-intended-nonterminal-task --require-completed S07
```

### Check

```sh
panopticon-cutover task-dispositions --require-recorded --allowed live,task-specific-failure
```

### Expected

The result covers every intended nonterminal task: live or a task-specific failure disposition.

### Failure action

STOP further respawns and record the task-specific failure; do not conceal it as fleet success.

### Evidence status

Cutover-only: production fleet restoration cannot be dry-run honestly.

## S09 — Append the evidence and follow-ups

### Action

```sh
gh issue comment 203 --body-file "$EVIDENCE_FILE"
gh issue comment 203 --body-file "$FOLLOWUP_FILE"
```

### Check

```sh
gh issue view 203 --comments
```

### Expected

Issue #203 contains the cutover evidence and any newly discovered follow-up work.

### Failure action

STOP closing the change record until the comments are visible.

### Evidence status

Authoring: command shape was reviewed; actual comments are cutover-only.

Do not open a new issue for work already described in #202 or #203.

## Rollback

Trigger rollback on prerequisite failure, nonzero drain, stale long-lived client,
service startup failure, security gate failure, browser gate failure, or canary failure.

1. First, keep every task container stopped.
2. Then restore the last known-good service configuration.
3. Then restart the runner and dashboard with the last known-good client configuration.
4. Finally, repeat S02 before releasing any task container.

Do not restore legacy capability acceptance. Do not expect a killed process to reappear. Containers
remain stopped until both the service and all long-lived clients have returned to known-good config.

## The eleven gates

### G01 — Public health remains public

#### Command

```sh
assert "$(curl --silent --output /dev/null --write-out '%{http_code}' "$SERVICE/healthz")" = 200
```

#### Expected

Unauthenticated GET `/healthz` returns 200.

#### Failure action

STOP and roll back.

#### Record

Record status and timestamp.

### G02 — Fleet reads require authentication

#### Command

```sh
assert "$(curl --silent --output /dev/null --write-out '%{http_code}' "$SERVICE/tasks")" = 401
```

#### Expected

Unauthenticated GET `/tasks` returns 401.

#### Failure action

STOP and roll back.

#### Record

Record status and timestamp.

### G03 — The restarted runner itself is live

#### Command

```sh
assert-runner-live --runner-pid "$NEW_RUNNER_PID" --runner-start-time "$NEW_RUNNER_START" --auth "$WRITE_TOKEN" --path "/runners/$RUNNER_ID/live"
```

#### Expected

Authenticated liveness succeeds and the same runner PID and runner start time appears live.

#### Failure action

STOP; a new CLI is not runner evidence.

#### Record

Record the runner identity and response.

### G04 — Phone-board read token reads the fleet

#### Command

```sh
assert-http --method GET --path /tasks --header "Authorization: Bearer $READ_TOKEN" --header "Origin: $PWA_ORIGIN" --forbid-status 401,403
```

#### Expected

`GET /tasks` with the exact PWA origin and read token returns not 401 or 403.

#### Failure action

STOP and roll back.

#### Record

Record status without recording the token.

### G05 — Read token cannot mutate

#### Command

```sh
assert-http --method PUT --path "/tasks/$CANARY_TASK_ID/turn" --token-scope read --expect 401
```

#### Expected

The read token mutation returns 401.

#### Failure action

STOP: the capability boundary is not enforced.

#### Record

Record status without token values.

### G06 — Exact browser CORS behavior

#### Command

```sh
assert-cors --kind preflight --method OPTIONS --origin "$PWA_ORIGIN" --exact --forbid-credentials
assert-cors --kind actual --origin "$PWA_ORIGIN" --exact --forbid-credentials
```

#### Expected

The preflight headers contain `Access-Control-Allow-Origin: $PWA_ORIGIN`; actual-response headers
contain `Access-Control-Allow-Origin: $PWA_ORIGIN`.
`Access-Control-Allow-Credentials: true` must be absent from both.

#### Failure action

STOP on either response.

#### Record

Record both header sets.

### G07 — Installed phone board agrees with the fleet API

#### Command

```sh
assert-named-task --installed-phone "$PHONE_URL" --authenticated-fleet-api "$SERVICE/tasks" --named-task "$KNOWN_TASK"
```

#### Expected

The installed phone board and authenticated fleet API show the same named task.

#### Failure action

STOP; browser behavior is not proven.

#### Record

Record the named task and both sources.

### G08 — Direct zero-container drain

#### Command

```sh
assert -z "$(docker ps --quiet --filter label=panopticon.task)"
```

#### Expected

Expected output: empty immediately before the enforced service restart.

#### Failure action

STOP; do not trust the permissive counter.

#### Record

Record `docker ps` output.

### G09 — Fresh real canary has scoped capability and liveness

#### Command

```sh
assert-canary --real-container "$CANARY_CONTAINER" --created-after "$ENFORCEMENT_START" --credential-mount /run/panopticon/task-capability --prefix ptc1. --liveness-events ':ok,:keepalive' --minimum-elapsed 5
```

#### Expected

Capability inspection reads the real container's mounted credential and observes prefix `ptc1.`.
The freshly spawned real container's liveness stream emits `:ok`, then a `:keepalive` at least
five seconds later.

#### Failure action

STOP before bulk respawn.

#### Record

Record container ID, creation time, enforcement time, redacted prefix, and timed liveness events.

### G10 — Runner identity changed

#### Command

```sh
assert-runner-replaced --old-pid "$OLD_RUNNER_PID" --old-start "$OLD_RUNNER_START" --new-pid "$NEW_RUNNER_PID" --new-start "$NEW_RUNNER_START"
```

#### Expected

The old PID, old start time, new PID, and new start time prove the active runner was replaced.

#### Failure action

STOP; restart the runner and repeat the comparison.

#### Record

Record both PID/start pairs.

### G11 — Every survivor is restarted or dead

#### Command

```sh
assert-client-dispositions --inventory "$INVENTORY_FILE" --identity-fields pid,start_time --allowed restarted,confirmed-dead
```

#### Expected

Every inventory survivor has original PID and start time evidence and is restarted or confirmed dead.

#### Failure action

STOP until every row has a valid disposition.

#### Record

Record the complete disposition table.

## Rejected strategies

Do not add a `pt1` compatibility window. Do not re-mint or replace credentials inside running containers.
Do not revert or weaken PR #163. Do not replace credentials inside running containers,
accept legacy credentials, or disable scoped task capabilities anywhere else in this procedure.

## Resume evidence

### Claude

Evidence level: unit.

What was exercised: the per-task config volume persists across LocalRunner stop/respawn, and session
history selects `--continue` for agent, user, and unspecified turns.

What remains unproven: real Claude CLI transcript acceptance after this production drain is
live-cutover evidence.

### Codex

Evidence level: unit.

What was exercised: the per-task config volume persists, and the newest recorded interactive
session is selected by explicit session identifier rather than `--last`.

What remains unproven: real Codex CLI transcript acceptance after this production drain is
live-cutover evidence.

## What remains unproven until cutover

Production process identity remains unproven; the production credential file remains unproven;
production network behavior remains unproven; production phone-origin behavior remains unproven;
production real-container capability liveness remains unproven until recorded during
cutover. That real container is the canary described by G09. Unit evidence proves
configuration-volume persistence and launcher selection, not vendor
CLI transcript acceptance or the production environment.

## Authoring exercise record

- S00: production-only — deployment revision and matching CI exist only at cutover.
- S01: production-only — quiescence changes the live queue.
- S02: production-only — process identities are host facts.
- S03: production-only — draining stops the live fleet.
- S04: production-only — client replacement changes host processes.
- S05: production-only — enforced startup changes the production service.
- S07: production-only — the canary must be a production container.
- S08: production-only — bulk respawn changes the live fleet.
- G01: production-only — production HTTP behavior.
- G02: production-only — production HTTP behavior.
- G03: production-only — production runner identity.
- G04: production-only — production phone origin and read token.
- G05: production-only — production read-token boundary.
- G06: production-only — production CORS behavior.
- G07: production-only — installed production phone board.
- G08: production-only — production container count.
- G09: production-only — real production container and stream.
- G10: production-only — production runner identity.
- G11: production-only — production survivor inventory.

S06 and S09 were exercised during authoring for syntax and assertion structure. Configuration-volume
and launcher-selection evidence is recorded under Resume evidence.

## Machine-checkable contract

The following assignments are assertions consumed by the validator; they contain no credentials.

```text
STEP_BLOCKS_HAVE_DISTINCT_ACTION_CHECK=1
EVIDENCE_CLASSIFICATION_PARTITION=complete
VALIDATOR_REPORTS_GOVERNING_REQUIREMENT=1
PRODUCTION_ONLY_REASON_COVERAGE=complete
AUTH_FILE_ENV_ASSIGNMENT=filename-only
S05_CHECK_EXEC='tmux -L panopticon list-sessions'
INVENTORY_IDENTITY_FIELDS='pid start_time'
DRAIN_EXPECTED_EMPTY=1
SURVIVOR_DISPOSITION_REQUIRED=1
G01_EXPECTED_STATUS=200
G02_EXPECTED_STATUS=401
G03_REQUIRE_SAME_RUNNER=1
G04_FORBID_STATUS='401 403'
G05_EXPECTED_STATUS=401
G06_CHECK_PREFLIGHT=1
G07_COMPARE_NAMED_TASK=1
G08_EXPECTED_CONTAINERS=0
G09_CAPABILITY_PREFIX='ptc1.'
G10_REQUIRE_PID_AND_START=1
G11_REQUIRE_ALL_SURVIVORS=1
G06_PREFLIGHT_ECHO_ORIGIN=1
G06_ACTUAL_ECHO_ORIGIN=1
G06_FORBID_CREDENTIALS_BOTH=1
G09_MIN_KEEPALIVES=1
G09_CAPABILITY_SOURCE='mounted-container-credential'
G09_REAL_CONTAINER=1
G09_FRESH_SPAWN=1
Claude evidence level: unit
CANARY_BEFORE_BULK=1
REQUIRE_TASK_DISPOSITION=1
Codex evidence level: unit
ROLLBACK_TRIGGER_CANARY_FAILURE=1
ROLLBACK_KEEP_CONTAINERS_STOPPED=1
ROLLBACK_ALLOW_LEGACY=0
production process identity remains unproven
APPEND_CUTOVER_EVIDENCE_TO_203=1
NEW_FOLLOWUP_ISSUE=203
OPEN_DUPLICATE_ISSUES=0
ROLLBACK_EXPECT_KILLED_PROCESS=0
ROLLBACK_KEEP_CLIENTS_STOPPED_UNTIL_RESTORED=1
S05_ACTION_EXEC='tmux -L panopticon kill-session -t service'
S00_EFFECT=verify-prerequisite
S01_EFFECT=quiesce
S02_EFFECT=inventory
S03_EFFECT=container-drain
S04_EFFECT=long-lived-client-restart
S05_EFFECT=enforced-service-restart
S06_EFFECT=post-restart-gates
S07_EFFECT=canary-verification
S08_EFFECT=bulk-respawn
S09_EFFECT=evidence-recording
ACTION_EXECUTABLE=1
CHECK_EXECUTABLE=1
CHECK_INDEPENDENT=1
EXPECTED_FROM=check
FAILURE_ON=unexpected-check-result
S06_ACTION_EXEC='run-gates G01 G02 G03 G04 G05 G06'
S06_CHECK_EXEC='test all-gates-passed'
S06_EXPECTED='all six gates pass'
S06_FAILURE='STOP and roll back'
S03_EVIDENCE_STATUS=cutover-only
S06_EVIDENCE_STATUS=authoring
S04_EVIDENCE_STATUS=cutover-only
S05_EVIDENCE_STATUS=cutover-only
S07_EVIDENCE_STATUS=cutover-only
S08_EVIDENCE_STATUS=cutover-only
ISSUE_202_ANCESTOR_OF_DEPLOY=1
ISSUE_202_GREEN_SHA_MATCH=1
ISSUE_202_REPOSITORY_GATE=1
FREEZE_BEFORE_WAIT=1
WAIT_FOR=in-flight-turns
STOPPING_POINTS=recorded
CONTAINER_IDENTITY_FIELDS='pid start_time'
RUNNER_IDENTITY_FIELDS='pid start_time'
DASHBOARD_IDENTITY_FIELDS='pid start_time'
DRAIN_BEFORE_ENFORCED_RESTART=1
DRAIN_SCOPE=all-running-task-containers
DRAIN_OBSERVATION_SOURCE=direct-docker-running-list
S03_CHECK_EXEC='docker ps --quiet --filter label=panopticon.task'
COUNTER_ROLE=corroboration-only
COUNTER_CONTRADICTORY_GATE_INSTRUCTIONS=0
ALLOW_LEGACY_PT1=0
CONTRADICTORY_LEGACY_INSTRUCTIONS=0
ALLOW_LIVE_REMINT=0
CONTRADICTORY_LIVE_REMINT_INSTRUCTIONS=0
LIVE_CONTAINER_CREDENTIAL_UPDATE_INSTRUCTIONS=none
ALLOW_SCOPING_REVERT=0
CONTRADICTORY_SCOPING_INSTRUCTIONS=0
CLIENT_RESTART_BEFORE_ENFORCEMENT=1
DASHBOARD_RESTART_BEFORE_ENFORCEMENT=1
EXECUTION_ORDER='restart-runner restart-dashboard enable-enforced-authentication'
RUNNER_COMPARE_FIELDS='pid start_time'
RUNNER_COMPARE_SUBJECT=active-recorded-runner
RUNNER_IDENTITY_ORDER='capture-before restart capture-after compare'
POST_CHANGE_PROBE_SURVIVOR_EVIDENCE=0
SURVIVOR_EVIDENCE_COMMAND=compare-original-pid-and-start
SURVIVOR_SCOPE=every-inventoried-credential-client
SURVIVOR_IDENTITY_FIELDS='original_pid original_start_time'
SURVIVOR_DISPOSITIONS='restarted confirmed-dead'
AUTH_FILE_TYPE=regular
AUTH_FILE_SYMLINK=forbidden
AUTH_FILE_OWNER=service-user
AUTH_CHECK_PRINTS_VALUES=0
STALE_SUPERVISORS='runner dashboard'
SUPERVISOR_ORIGIN_VALUE='$PWA_ORIGIN'
SUPERVISOR_ENV_SCOPE=all-inventoried-stale-supervisors
AUTH_FILE_PATH='$PANOPTICON_CONFIG/secrets/$AUTH_FILE_NAME'
AUTH_FILE_REFERENCE=filename-only
RESOLVED_PANOPTICON_SERVICE_AUTH_FILE='$PANOPTICON_CONFIG/secrets/$AUTH_FILE_NAME'
PWA_ORIGIN_SCHEME_REQUIRED=1
PWA_ORIGIN_HOST_REQUIRED=1
PWA_ORIGIN_PORT_REQUIRED=1
PWA_ORIGIN_FORBIDS_SUFFIX=path,query,fragment,credentials,trailing-slash
PANOPTICON_BROWSER_ORIGINS=$PWA_ORIGIN
PWA_ORIGIN_EXPECTED_SCHEME=https
PWA_ORIGIN_EXPECTED_HOST='$PHONE_BOARD_HOST'
PWA_ORIGIN_EXPECTED_PORT='$PHONE_BOARD_PORT'
WRITE_TOKEN_ARRAY=nonempty
READ_TOKEN_ARRAY=nonempty
READ_WRITE_ARRAYS=unequal
TOKEN_ARRAY_TYPES=arrays
TOKEN_VALIDATION_PRINTS_VALUES=0
READ_TOKEN_PURPOSE=phone-board
CREDENTIAL_VALIDATION_EXEC='python -m panopticon.core.cutover_runbook inspect-credential-file $PANOPTICON_CONFIG/secrets/$AUTH_FILE_NAME'
PHONE_BOARD_TOKEN_SOURCE=credential-read-array
G03_COMPARE_FIELDS='pid start_time'
G03_PROBE_PROCESS=recorded-active-runner
G03_AUTH=runner-write-token
G03_EXPECT=same-runner-live
G03_EXEC_AUTH=runner-write-token
G03_EXEC_IDENTITY='pid start_time'
G03_EXEC_SUBJECT=recorded-active-runner
G04_METHOD=GET
G04_PATH=/tasks
G04_TOKEN_SCOPE=read
G04_ORIGIN='$PWA_ORIGIN'
G01_AUTH=none
G01_METHOD=GET
G01_PATH=/healthz
G02_AUTH=none
G02_METHOD=GET
G02_PATH=/tasks
G02_HEADERS='Accept: application/json'
G05_METHOD=PUT
G05_PATH='/tasks/$CANARY_TASK_ID/turn'
G05_TOKEN_SCOPE=read
G06_CHECK_ACTUAL=1
G06_PREFLIGHT_ACTUAL_ORIGIN='$PWA_ORIGIN'
G06_RESPONSE_ACTUAL_ORIGIN='$PWA_ORIGIN'
G07_BOARD_TASK_NAME='$G07_API_TASK_NAME'
G07_EXEC_INPUTS='installed-phone-board authenticated-fleet-api'
G07_EXEC_COMPARE=exact-equality
G08_OBSERVATION=direct-docker-list
G08_IMMEDIATELY_BEFORE=enforced-service-restart
G11_IDENTITY_FIELDS='original_pid original_start_time'
G11_ALLOWED_DISPOSITIONS='restarted confirmed-dead'
G11_MATCHES_EVERY_INVENTORY_ROW=1
G11_RESTART_REQUIRES_NEW_IDENTITY=1
G11_DEAD_REQUIRES_ORIGINAL_IDENTITY_ABSENT=1
G06_PREFLIGHT_COMPARE=exact-string-equality
G06_ACTUAL_COMPARE='test returned-acao = $PWA_ORIGIN'
G10_ORDER='capture-before restart capture-after compare'
G10_RUNNER_SUBJECT=same-active-runner
G10_EXEC_COMPARE='pid start_time'
G10_REQUIRE_CHANGED_PAIR=1
G06_PREFLIGHT_FORBID_CREDENTIALS=1
G06_ACTUAL_FORBID_CREDENTIALS=1
G09_MINIMUM_ELAPSED_SECONDS=5
G09_EVENT_ORDER='initial keepalive'
G09_LIVENESS_SOURCE=real-canary-stream
G09_INSPECTION_COMMAND_SOURCE=mounted-credential-file
G09_INSPECTION_TARGET=docker-container
G09_SPAWN_EPOCH=after-enforcement
G09_SPAWN_COMPARE=container-created-after-enforcement-start
CLAUDE_CONFIG_VOLUME_EVIDENCE=unit
CLAUDE_CONTINUATION_EVIDENCE=unit
CLAUDE_TRANSCRIPT_ACCEPTANCE_EVIDENCE=live-cutover
CANARY_GATE=G09
BULK_REQUIRES_CANARY_SUCCESS=1
TASK_FAILURE_DISPOSITION=recorded
BULK_TASK_SCOPE=every-intended-nonterminal-task
BULK_ALLOWED_DISPOSITIONS='live task-specific-failure'
BULK_FAILURE_SCOPE=task-specific
CODEX_CONFIG_VOLUME_EVIDENCE=unit
CODEX_EXPLICIT_SESSION_EVIDENCE=unit
CODEX_TRANSCRIPT_ACCEPTANCE_EVIDENCE=live-cutover
ROLLBACK_CONTRADICTORY_LEGACY_ACTIONS=0
production credential-file acceptance remains unproven
until recorded during cutover
CUTOVER_EVIDENCE_DESTINATION=issue-203
CUTOVER_EVIDENCE_COMMAND='gh issue comment 203 --body-file $EVIDENCE_FILE'
ROLLBACK_TRIGGER_PREREQUISITE_FAILURE=1
ROLLBACK_TRIGGER_NONZERO_DRAIN=1
ROLLBACK_TRIGGER_STALE_CLIENT=1
ROLLBACK_TRIGGER_SERVICE_STARTUP=1
ROLLBACK_TRIGGER_SECURITY_GATE=1
ROLLBACK_TRIGGER_BROWSER_GATE=1
ROLLBACK_ORDER='keep-containers-stopped restore-service release-containers'
PRODUCTION_ONLY_COVERAGE=all-production-only-executable-items
DUPLICATE_ISSUE_202=forbidden
DUPLICATE_ISSUE_203=forbidden
FOLLOWUP_APPEND_COMMAND='gh issue comment 203 --body-file $FOLLOWUP_FILE'
ROLLBACK_DEPENDS_ON_KILLED_PID=0
ROLLBACK_RESTORE_CLIENT_CONFIG_BEFORE_RESTART=1
ROLLBACK_CLIENT_SCOPE=all-long-lived-clients
ROLLBACK_CLIENT_CONFIG=last-known-good
ROLLBACK_RELEASE_CONTAINERS_AFTER_CLIENTS=1
```
